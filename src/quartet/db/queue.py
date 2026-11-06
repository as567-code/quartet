"""Lease queue: SKIP LOCKED claim, heartbeat, release, reaper.

All lease arithmetic uses the database clock (``now()``), never the host clock. Every function
commits its own transaction. Core UPDATEs must set ``updated_at`` themselves (the ORM
``onupdate`` hook does not fire for Core SQL).
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
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

_REAP_SQL = sa.text(
    """
    UPDATE tasks SET lease_owner = NULL, lease_expires_at = NULL,
                     attempt = attempt + 1, updated_at = now()
    WHERE lease_owner IS NOT NULL AND lease_expires_at <= now()
    RETURNING id
    """
)


@dataclass(frozen=True)
class ClaimedTask:
    task_id: UUID
    state: TaskState
    version: int
    attempt: int
    lease_expires_at: datetime


def claim(session: Session, worker_id: str, lease_seconds: int) -> ClaimedTask | None:
    """Atomically lease the best runnable task, or return None. Commits."""
    row = session.execute(
        _CLAIM_SQL,
        {"worker": worker_id, "lease": lease_seconds, "runnable": _RUNNABLE_VALUES},
    ).one_or_none()
    session.commit()
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
    result = session.execute(_HEARTBEAT_SQL, {"s": lease_seconds, "id": task_id, "w": worker_id})
    session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


def release(session: Session, task_id: UUID, worker_id: str) -> None:
    """Clear the lease if (and only if) ``worker_id`` owns it. Commits."""
    session.execute(_RELEASE_SQL, {"id": task_id, "w": worker_id})
    session.commit()


def reap_expired(session: Session) -> list[UUID]:
    """Free every expired lease, bumping ``attempt``. Returns reaped ids. Commits."""
    ids = [r.id for r in session.execute(_REAP_SQL)]
    session.commit()
    return ids
