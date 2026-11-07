"""Task state machine (PRD §9.2) with compare-and-set transitions.

``transition`` never commits: the caller owns the transaction, and the state UPDATE and the
event INSERT always run inside it together.
"""

import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from quartet.domain.events import check_event_payload
from quartet.domain.states import TaskState

S = TaskState

TERMINAL: frozenset[TaskState] = frozenset({S.REJECTED, S.MERGED, S.CLOSED})

_BASE_EDGES: dict[TaskState, frozenset[TaskState]] = {
    S.QUEUED: frozenset({S.SCOPING}),
    S.SCOPING: frozenset({S.PLANNING, S.REJECTED}),
    S.PLANNING: frozenset({S.PLAN_REVIEW, S.WAITING_LEASES}),
    S.PLAN_REVIEW: frozenset({S.WAITING_LEASES}),
    S.WAITING_LEASES: frozenset({S.IMPLEMENTING}),
    S.IMPLEMENTING: frozenset({S.TESTING, S.WAITING_LEASES}),
    S.TESTING: frozenset({S.REVIEWING, S.IMPLEMENTING}),
    S.REVIEWING: frozenset({S.IMPLEMENTING, S.INTEGRATING}),
    S.INTEGRATING: frozenset({S.PR_DRAFT, S.AWAITING_HUMAN}),
    S.PR_DRAFT: frozenset({S.AWAITING_HUMAN}),
    S.AWAITING_HUMAN: frozenset({S.MERGED}),
    # Resume: any non-terminal state; the DB check narrows it to the recorded resume_state.
    S.NEEDS_HUMAN: frozenset(set(S) - TERMINAL),
}


def _build() -> Mapping[TaskState, frozenset[TaskState]]:
    table: dict[TaskState, frozenset[TaskState]] = {}
    for state in TaskState:
        if state in TERMINAL:
            table[state] = frozenset()
            continue
        edges = _BASE_EDGES.get(state, frozenset()) | {S.NEEDS_HUMAN, S.CLOSED}
        table[state] = frozenset(edges - {state})
    return MappingProxyType(table)


ALLOWED: Mapping[TaskState, frozenset[TaskState]] = _build()


class IllegalTransition(Exception):
    """The edge is not in the state graph (or a NEEDS_HUMAN resume targets the wrong state)."""


class StaleTransition(Exception):
    """The task's state or version no longer matches what the caller expected."""


def check_transition(from_: TaskState, to: TaskState) -> None:
    if to not in ALLOWED[from_]:
        raise IllegalTransition(f"{from_.value} -> {to.value} is not allowed")


_LOCK_SQL = sa.text(
    "SELECT state::text AS state, version, resume_state::text AS resume_state "
    "FROM tasks WHERE id = :id FOR UPDATE"
)

_UPDATE_SQL = sa.text(
    "UPDATE tasks SET state = CAST(:to AS task_state), version = version + 1, "
    "updated_at = now(), "
    "resume_state = CASE WHEN :to = 'NEEDS_HUMAN' THEN CAST(:from AS task_state) "
    "WHEN :from = 'NEEDS_HUMAN' THEN NULL ELSE resume_state END "
    "WHERE id = :id AND state = CAST(:from AS task_state) AND version = :v "
    "RETURNING version"
)

_EVENT_SQL = sa.text(
    "INSERT INTO events (task_id, type, payload) VALUES (:id, :type, :payload)"
).bindparams(sa.bindparam("payload", type_=postgresql.JSONB))


def transition(
    session: Session,
    task_id: uuid.UUID,
    from_: TaskState,
    to: TaskState,
    expected_version: int,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
) -> int:
    """Move a task ``from_`` -> ``to`` iff it is still at ``expected_version``; return the new one.

    Raises ``IllegalTransition`` (bad edge / wrong resume target), ``StaleTransition`` (lost
    the race) or ``ValueError`` (event payload over the size cap, raised before any SQL).
    Does not commit. Lease-agnostic by design: lease-less edges (``AWAITING_HUMAN -> MERGED``,
    CLI actions) use it too; a reaped worker is fenced because reaping bumps ``version``.
    """
    check_transition(from_, to)
    event_payload = check_event_payload({**(payload or {}), "from": from_.value, "to": to.value})

    if from_ is TaskState.NEEDS_HUMAN and to is not TaskState.CLOSED:
        row = session.execute(_LOCK_SQL, {"id": task_id}).one_or_none()
        if row is None or row.state != from_.value or row.version != expected_version:
            raise StaleTransition(
                f"task {task_id} is no longer at {from_.value} v{expected_version}"
            )
        if row.resume_state != to.value:
            raise IllegalTransition(
                f"task {task_id} may only resume to {row.resume_state}, not {to.value}"
            )

    new_version = session.execute(
        _UPDATE_SQL,
        {"to": to.value, "from": from_.value, "id": task_id, "v": expected_version},
    ).scalar_one_or_none()
    if new_version is None:
        raise StaleTransition(f"task {task_id} is no longer at {from_.value} v{expected_version}")

    session.execute(_EVENT_SQL, {"id": task_id, "type": event_type, "payload": event_payload})
    return int(new_version)
