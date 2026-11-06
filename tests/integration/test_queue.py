import re
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
import time_machine
from psycopg import sql
from sqlalchemy.orm import Session

from quartet.db.queue import _CLAIM_SQL, _RUNNABLE_VALUES, ClaimedTask, heartbeat, reap_expired
from quartet.db.queue import claim as _claim
from quartet.db.queue import release as _release
from quartet.domain.states import RUNNABLE, TaskState
from quartet.domain.transitions import StaleTransition, transition
from tests.integration._claimers import run_claimers

pytestmark = pytest.mark.integration


def claim(session: Session, worker: str, lease: int) -> ClaimedTask:
    task = _claim(session, worker, lease)
    assert task is not None
    return task


def _one(engine: sa.Engine, stmt: str, **params: Any) -> sa.Row[Any]:
    """Read through a separate connection so the queue session never holds a transaction."""
    with engine.connect() as c:
        return c.execute(sa.text(stmt), params).one()


def _all(engine: sa.Engine, stmt: str, **params: Any) -> list[sa.Row[Any]]:
    with engine.connect() as c:
        return list(c.execute(sa.text(stmt), params))


def _expire_all(engine: sa.Engine) -> None:
    with engine.begin() as c:
        c.execute(sa.text("UPDATE tasks SET lease_expires_at = now() - interval '1 second'"))


def _reaped_events(engine: sa.Engine) -> list[sa.Row[Any]]:
    return _all(
        engine,
        "SELECT task_id, payload FROM events WHERE type = 'lease_reaped' ORDER BY id",
    )


def test_claim_orders_by_priority_then_age(app_session, make_tasks):
    _low, high = make_tasks(n=2, priorities=[200, 10])
    assert claim(app_session, "w1", 30).task_id == high


def test_claim_ties_break_by_age(app_session, make_tasks):
    first, _second = make_tasks(n=2)
    assert claim(app_session, "w1", 30).task_id == first


def test_claim_sets_lease_and_updated_at(app_engine, app_session, make_tasks):
    (tid,) = make_tasks(n=1)
    before = _one(app_engine, "SELECT updated_at FROM tasks").updated_at
    c = claim(app_session, "w1", 30)
    row = _one(
        app_engine,
        "SELECT lease_owner, lease_expires_at, updated_at FROM tasks WHERE id = :i",
        i=tid,
    )
    assert row.lease_owner == "w1" and row.lease_expires_at == c.lease_expires_at
    assert row.updated_at > before
    assert _claim(app_session, "w2", 30) is None


def test_each_task_claimed_exactly_once_across_processes(app_url, make_tasks):
    ids = {str(i) for i in make_tasks(n=200)}
    claimed = run_claimers(app_url, processes=8)
    assert len(claimed) == len(set(claimed)) == 200 and set(claimed) == ids


def test_expired_leases_reaped_exactly_once_across_processes(app_engine, app_url, make_tasks):
    """Every claimer reaps first: concurrent reapers must not double-bump or double-log."""
    ids = make_tasks(n=40)
    with Session(app_engine) as s:
        for i in range(40):
            claim(s, f"old{i}", 30)
    _expire_all(app_engine)
    claimed = run_claimers(app_url, processes=8)
    assert sorted(claimed) == sorted(str(i) for i in ids)
    rows = _all(app_engine, "SELECT version, attempt, lease_owner FROM tasks")
    assert {(r.version, r.attempt) for r in rows} == {(1, 1)}
    assert all(r.lease_owner is not None and not r.lease_owner.startswith("old") for r in rows)
    events = _reaped_events(app_engine)
    assert sorted(str(e.task_id) for e in events) == sorted(str(i) for i in ids)


def test_heartbeat_extends_live_lease(app_engine, app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 5)
    assert heartbeat(app_session, c.task_id, "w1", 60) is True
    new = _one(app_engine, "SELECT lease_expires_at FROM tasks").lease_expires_at
    assert new > c.lease_expires_at


def test_heartbeat_by_other_worker_fails(app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 5)
    assert heartbeat(app_session, c.task_id, "w2", 60) is False


def test_heartbeat_on_expired_but_unreaped_lease_returns_false(app_engine, app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 30)
    _expire_all(app_engine)
    assert heartbeat(app_session, c.task_id, "w1", 60) is False
    assert _one(app_engine, "SELECT lease_owner FROM tasks").lease_owner == "w1"


def test_heartbeat_after_reap_returns_false(app_engine, app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 1)
    _expire_all(app_engine)
    assert reap_expired(app_session) == [c.task_id]
    assert heartbeat(app_session, c.task_id, "w1", 60) is False
    assert _one(app_engine, "SELECT lease_owner FROM tasks").lease_owner is None


def test_reap_increments_attempt(app_engine, app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 1)
    _expire_all(app_engine)
    reap_expired(app_session)
    assert claim(app_session, "w2", 30).attempt == c.attempt + 1


def test_reap_bumps_version_and_fences_the_old_holder(app_engine, app_session, make_tasks):
    make_tasks(n=1)
    old = claim(app_session, "w1", 30)
    _expire_all(app_engine)
    assert reap_expired(app_session) == [old.task_id]
    new = claim(app_session, "w2", 30)
    assert new.task_id == old.task_id and new.version == old.version + 1
    with Session(app_engine) as s:
        with pytest.raises(StaleTransition):
            transition(s, old.task_id, old.state, TaskState.SCOPING, old.version, "w1_late")
        s.rollback()
        assert transition(s, new.task_id, new.state, TaskState.SCOPING, new.version, "w2") == (
            new.version + 1
        )
        s.commit()


def test_reap_writes_one_lease_reaped_event_per_task(app_engine, app_session, make_tasks):
    ids = make_tasks(n=3)
    owners = {claim(app_session, f"w{i}", 30).task_id: f"w{i}" for i in range(3)}
    assert set(owners) == set(ids)
    _expire_all(app_engine)
    assert sorted(reap_expired(app_session)) == sorted(ids)
    events = _reaped_events(app_engine)
    assert sorted(e.task_id for e in events) == sorted(ids)
    for e in events:
        assert e.payload == {"lease_owner": owners[e.task_id], "attempt": 1}
    assert reap_expired(app_session) == []
    assert len(_reaped_events(app_engine)) == 3


@pytest.mark.parametrize(
    "state", [TaskState.MERGED, TaskState.AWAITING_HUMAN, TaskState.NEEDS_HUMAN]
)
def test_reap_bumps_attempt_only_for_runnable_states(app_engine, app_session, make_tasks, state):
    (tid,) = make_tasks(n=1, state=state)
    with app_engine.begin() as c:
        c.execute(
            sa.text(
                "UPDATE tasks SET lease_owner = 'stale', "
                "lease_expires_at = now() - interval '1 second' WHERE id = :i"
            ),
            {"i": tid},
        )
    assert reap_expired(app_session) == [tid]
    row = _one(
        app_engine,
        "SELECT state::text AS state, version, attempt, lease_owner, lease_expires_at "
        "FROM tasks WHERE id = :i",
        i=tid,
    )
    assert (row.state, row.version, row.attempt) == (state.value, 1, 0)
    assert row.lease_owner is None and row.lease_expires_at is None
    (event,) = _reaped_events(app_engine)
    assert event.payload == {"lease_owner": "stale", "attempt": 0}


def test_claim_reaps_expired_leases_without_a_reaper(app_engine, app_session, make_tasks):
    make_tasks(n=1)
    old = claim(app_session, "w1", 30)
    _expire_all(app_engine)
    new = claim(app_session, "w2", 30)
    assert new.task_id == old.task_id
    assert (new.version, new.attempt) == (old.version + 1, old.attempt + 1)
    (event,) = _reaped_events(app_engine)
    assert event.task_id == old.task_id and event.payload["lease_owner"] == "w1"
    assert _one(app_engine, "SELECT lease_owner FROM tasks").lease_owner == "w2"


def test_release_clears_only_own_lease(app_engine, app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 30)
    _release(app_session, c.task_id, "w2")
    assert _one(app_engine, "SELECT lease_owner FROM tasks").lease_owner == "w1"
    _release(app_session, c.task_id, "w1")
    row = _one(app_engine, "SELECT lease_owner, lease_expires_at FROM tasks")
    assert row.lease_owner is None and row.lease_expires_at is None
    assert claim(app_session, "w2", 30).task_id == c.task_id


def test_lease_logic_ignores_host_clock(app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 3600)
    with time_machine.travel(datetime(2100, 1, 1, tzinfo=UTC), tick=False):
        assert reap_expired(app_session) == []
        assert heartbeat(app_session, c.task_id, "w1", 60) is True


def test_terminal_and_needs_human_tasks_are_never_claimed(app_session, make_tasks):
    make_tasks(n=1, state=TaskState.MERGED)
    make_tasks(n=1, state=TaskState.NEEDS_HUMAN)
    assert _claim(app_session, "w1", 30) is None


@pytest.mark.parametrize("state", sorted(RUNNABLE))
def test_every_runnable_state_is_claimable(app_session, make_tasks, state):
    make_tasks(n=1, state=state)
    claim(app_session, "w1", 30)


# ---- dedicated-session discipline ----


@pytest.mark.parametrize("op", ["claim", "heartbeat", "release", "reap_expired"])
def test_queue_ops_refuse_a_session_with_an_open_transaction(app_engine, seeded_task, op):
    calls = {
        "claim": lambda s: _claim(s, "w1", 30),
        "heartbeat": lambda s: heartbeat(s, seeded_task, "w1", 30),
        "release": lambda s: _release(s, seeded_task, "w1"),
        "reap_expired": reap_expired,
    }
    with Session(app_engine) as s:
        transition(s, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 0, "uncommitted")
        with pytest.raises(RuntimeError, match="dedicated session with no open transaction"):
            calls[op](s)
        # The queue op neither committed nor discarded the caller's transaction.
        assert s.in_transaction()
        row = _one(app_engine, "SELECT state::text AS state, version FROM tasks")
        assert (row.state, row.version) == ("QUEUED", 0)
        s.rollback()
    assert _all(app_engine, "SELECT 1 FROM events WHERE type = 'uncommitted'") == []


def test_queue_op_rolls_back_and_reraises_on_error(app_engine, app_session, make_tasks):
    make_tasks(n=1)
    old = claim(app_session, "w1", 30)
    _expire_all(app_engine)
    with pytest.raises(sa.exc.DBAPIError):
        _claim(app_session, "w2", 10**18)  # lease end overflows the timestamp range
    assert not app_session.in_transaction()
    # The reap that ran first in the same transaction was rolled back with it.
    row = _one(app_engine, "SELECT lease_owner, version FROM tasks")
    assert (row.lease_owner, row.version) == ("w1", old.version)
    assert _reaped_events(app_engine) == []
    assert claim(app_session, "w2", 30).version == old.version + 1


# ---- planner uses the partial claim index at realistic scale (FR-m11) ----


def _plan(engine: sa.Engine, stmt: str, params: dict[str, Any]) -> str:
    explain = sql.SQL("EXPLAIN {}").format(sql.SQL(stmt)).as_string()
    with engine.connect() as c:
        return "\n".join(r[0] for r in c.execute(sa.text(explain), params))


def test_claim_query_uses_partial_index_at_scale(app_engine, migrated_db, repo_id):
    with app_engine.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO tasks (repo_id, source, title, state, priority) "
                "SELECT :r, 'manual', 't' || g, "
                "CAST(CASE WHEN g % 10 = 0 THEN 'QUEUED' ELSE 'MERGED' END AS task_state), "
                "g % 7 FROM generate_series(1, 20000) AS g"
            ),
            {"r": repo_id},
        )
    owner = sa.create_engine(migrated_db.migrator_url)  # ANALYZE needs the table owner
    try:
        with owner.begin() as c:
            c.execute(sa.text("ANALYZE tasks"))
    finally:
        owner.dispose()
    match = re.search(r"WHERE id = \((.*?)\)\s*RETURNING", _CLAIM_SQL.text, re.DOTALL)
    assert match is not None, "could not locate the inner SELECT of _CLAIM_SQL"
    inner = match.group(1)
    assert inner.strip().startswith("SELECT id FROM tasks")
    inner_plan = _plan(app_engine, inner, {"runnable": _RUNNABLE_VALUES})
    assert "ix_tasks_claimable" in inner_plan, inner_plan
    full_plan = _plan(
        app_engine, _CLAIM_SQL.text, {"worker": "w", "lease": 30, "runnable": _RUNNABLE_VALUES}
    )
    assert "ix_tasks_claimable" in full_plan, full_plan
