"""Lease queue: SKIP LOCKED claim, heartbeat, release, reaper.

All lease arithmetic uses the database clock (``now()``), never the host clock. Core UPDATEs
must set ``updated_at`` themselves (the ORM ``onupdate`` hook does not fire for Core SQL).

Session discipline (ruling R32): every function here owns exactly one transaction. It must be
given a dedicated session with no open transaction (else ``RuntimeError``, and the caller's
transaction is left untouched), commits on success, and rolls back then re-raises on any error.
Never share that session with ``transition()`` or the repository helpers, whose callers own
the transaction.

Fencing (ruling R34): reaping an expired lease bumps ``version``, the compare-and-set token that
``transition()`` checks, so a worker whose lease was reaped can no longer move the task.
``claim()`` reaps first, in the same transaction, so recovery never waits for a reaper loop.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from quartet.domain.states import RUNNABLE, TaskState

_RUNNABLE_VALUES: list[str] = sorted(s.value for s in RUNNABLE)

_CLAIM_SQL = sa.text(
    """
    UPDATE tasks SET lease_owner = :worker,
                     lease_expires_at = now() + make_interval(secs => :lease),
                     updated_at = now()
    WHERE id = (
      SELECT id FROM tasks
      WHERE lease_owner IS NULL AND state = ANY(CAST(:runnable AS task_state[]))
      ORDER BY priority ASC, created_at ASC
      LIMIT 1
      FOR UPDATE SKIP LOCKED)
    RETURNING id, state, version, attempt, lease_expires_at
    """
)

_HEARTBEAT_SQL = sa.text(
    """
    UPDATE tasks SET lease_expires_at = now() + make_interval(secs => :s),
                     updated_at = now()
    WHERE id = :id AND lease_owner = :w AND lease_expires_at > now()
    """
)

_RELEASE_SQL = sa.text(
    """
    UPDATE tasks SET lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
    WHERE id = :id AND lease_owner = :w
    """
)

# Lock expired leases first (SKIP LOCKED: concurrent reapers split the rows, never double-reap),
# then clear them, bump the fencing token and (for runnable tasks only) the attempt, and log one
# lease_reaped event per task. One statement, one transaction.
_REAP_SQL = sa.text(
    """
    WITH expired AS (
      SELECT id, lease_owner FROM tasks
      WHERE lease_owner IS NOT NULL AND lease_expires_at <= now()
      ORDER BY id
      FOR UPDATE SKIP LOCKED
    ), reaped AS (
      UPDATE tasks AS t
      SET lease_owner = NULL, lease_expires_at = NULL,
          version = t.version + 1,
          attempt = t.attempt
                    + CASE WHEN t.state = ANY(CAST(:runnable AS task_state[])) THEN 1 ELSE 0 END,
          updated_at = now()
      FROM expired AS e
      WHERE t.id = e.id
      RETURNING t.id, e.lease_owner AS lease_owner_before, t.attempt
    )
    INSERT INTO events (task_id, type, payload)
    SELECT id, 'lease_reaped',
           jsonb_build_object('lease_owner', lease_owner_before, 'attempt', attempt)
    FROM reaped
    RETURNING task_id
    """
)

_NEEDS_DEDICATED = "queue operations need a dedicated session with no open transaction"


@dataclass(frozen=True)
class ClaimedTask:
    """A leased task.

    ``version`` is the compare-and-set and fencing token: pass it to ``transition()`` as
    ``expected_version``. Reaping this lease bumps it, so after a reap the old holder's
    transitions fail with ``StaleTransition``.
    """

    task_id: UUID
    state: TaskState
    version: int
    attempt: int
    lease_expires_at: datetime


@contextmanager
def _own_transaction(session: Session) -> Iterator[None]:
    if session.in_transaction():
        raise RuntimeError(_NEEDS_DEDICATED)
    try:
        yield
        session.commit()
    except BaseException:
        session.rollback()
        raise


def _reap(session: Session) -> list[UUID]:
    return [r.task_id for r in session.execute(_REAP_SQL, {"runnable": _RUNNABLE_VALUES})]


def claim(session: Session, worker_id: str, lease_seconds: int) -> ClaimedTask | None:
    """Reap expired leases, then lease the best runnable task, or return None. Commits."""
    with _own_transaction(session):
        _reap(session)
        row = session.execute(
            _CLAIM_SQL,
            {"worker": worker_id, "lease": lease_seconds, "runnable": _RUNNABLE_VALUES},
        ).one_or_none()
    if row is None:
        return None
    return ClaimedTask(
        task_id=row.id,
        state=TaskState(row.state),
        version=row.version,
        attempt=row.attempt,
        lease_expires_at=row.lease_expires_at,
    )


def heartbeat(session: Session, task_id: UUID, worker_id: str, lease_seconds: int) -> bool:
    """Extend a still-live lease owned by ``worker_id``. False if lost or expired. Commits."""
    with _own_transaction(session):
        result = session.execute(
            _HEARTBEAT_SQL, {"s": lease_seconds, "id": task_id, "w": worker_id}
        )
    return bool(cast(CursorResult[Any], result).rowcount)


def release(session: Session, task_id: UUID, worker_id: str) -> None:
    """Clear the lease if (and only if) ``worker_id`` owns it. Commits."""
    with _own_transaction(session):
        session.execute(_RELEASE_SQL, {"id": task_id, "w": worker_id})


def reap_expired(session: Session) -> list[UUID]:
    """Free every expired lease (bumping ``version``; ``attempt`` for runnable tasks only) and
    log a ``lease_reaped`` event for each. Returns the reaped ids. Commits."""
    with _own_transaction(session):
        return _reap(session)
