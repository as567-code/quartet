from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
import time_machine
from sqlalchemy.orm import Session

from quartet.db.queue import ClaimedTask, heartbeat, reap_expired, release
from quartet.db.queue import claim as _claim
from quartet.domain.states import RUNNABLE, TaskState
from tests.integration._claimers import run_claimers

pytestmark = pytest.mark.integration


def claim(session: Session, worker: str, lease: int) -> ClaimedTask:
    task = _claim(session, worker, lease)
    assert task is not None
    return task


def _expire_all(session: Session) -> None:
    session.execute(sa.text("UPDATE tasks SET lease_expires_at = now() - interval '1 second'"))
    session.commit()


def test_claim_orders_by_priority_then_age(app_session, make_tasks):
    _low, high = make_tasks(n=2, priorities=[200, 10])
    assert claim(app_session, "w1", 30).task_id == high


def test_claim_ties_break_by_age(app_session, make_tasks):
    first, _second = make_tasks(n=2)
    assert claim(app_session, "w1", 30).task_id == first


def test_claim_sets_lease_and_updated_at(app_session, make_tasks):
    (tid,) = make_tasks(n=1)
    before = app_session.execute(sa.text("SELECT updated_at FROM tasks")).scalar_one()
    c = claim(app_session, "w1", 30)
    row = app_session.execute(
        sa.text("SELECT lease_owner, lease_expires_at, updated_at FROM tasks WHERE id = :i"),
        {"i": tid},
    ).one()
    assert row.lease_owner == "w1" and row.lease_expires_at == c.lease_expires_at
    assert row.updated_at > before
    assert _claim(app_session, "w2", 30) is None


def test_each_task_claimed_exactly_once_across_processes(app_url, make_tasks):
    ids = {str(i) for i in make_tasks(n=200)}
    claimed = run_claimers(app_url, processes=8)
    assert len(claimed) == len(set(claimed)) == 200 and set(claimed) == ids


def test_heartbeat_extends_live_lease(app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 5)
    assert heartbeat(app_session, c.task_id, "w1", 60) is True
    new = app_session.execute(sa.text("SELECT lease_expires_at FROM tasks")).scalar_one()
    assert new > c.lease_expires_at


def test_heartbeat_by_other_worker_fails(app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 5)
    assert heartbeat(app_session, c.task_id, "w2", 60) is False


def test_heartbeat_after_reap_returns_false(app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 1)
    _expire_all(app_session)
    assert reap_expired(app_session) == [c.task_id]
    assert heartbeat(app_session, c.task_id, "w1", 60) is False
    assert app_session.execute(sa.text("SELECT lease_owner FROM tasks")).scalar() is None


def test_reap_increments_attempt(app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 1)
    _expire_all(app_session)
    reap_expired(app_session)
    assert claim(app_session, "w2", 30).attempt == c.attempt + 1


def test_release_clears_only_own_lease(app_session, make_tasks):
    make_tasks(n=1)
    c = claim(app_session, "w1", 30)
    release(app_session, c.task_id, "w2")
    owner = app_session.execute(sa.text("SELECT lease_owner FROM tasks")).scalar_one()
    assert owner == "w1"
    release(app_session, c.task_id, "w1")
    row = app_session.execute(sa.text("SELECT lease_owner, lease_expires_at FROM tasks")).one()
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
