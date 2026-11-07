import threading
import uuid

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from quartet.db.session import session_scope
from quartet.domain.states import TaskState
from quartet.domain.transitions import (
    ALLOWED,
    IllegalTransition,
    StaleTransition,
    transition,
)

pytestmark = pytest.mark.integration


def new_task(session: Session, repo_id: uuid.UUID) -> uuid.UUID:
    tid: uuid.UUID = session.execute(
        sa.text(
            "INSERT INTO tasks (repo_id, source, title, state) "
            "VALUES (:r, 'manual', 't', 'QUEUED') RETURNING id"
        ),
        {"r": repo_id},
    ).scalar_one()
    return tid


def test_cas_increments_version_and_logs_event(app_session, seeded_task):
    assert (
        transition(
            app_session, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 0, "scoping_started"
        )
        == 1
    )
    ev = app_session.execute(
        sa.text("SELECT type, payload FROM events WHERE task_id=:t"), {"t": seeded_task}
    ).one()
    assert ev.type == "scoping_started" and ev.payload["to"] == "SCOPING"
    assert ev.payload["from"] == "QUEUED"


def test_caller_payload_is_merged_but_cannot_override_from_to(app_session, seeded_task):
    transition(
        app_session,
        seeded_task,
        TaskState.QUEUED,
        TaskState.SCOPING,
        0,
        "x",
        {"note": "hi", "from": "FAKE", "to": "FAKE"},
    )
    payload = app_session.execute(
        sa.text("SELECT payload FROM events WHERE task_id=:t"), {"t": seeded_task}
    ).scalar_one()
    assert payload == {"note": "hi", "from": "QUEUED", "to": "SCOPING"}


def test_updated_at_is_bumped(app_session, seeded_task):
    before = app_session.execute(
        sa.text("SELECT updated_at FROM tasks WHERE id=:t"), {"t": seeded_task}
    ).scalar_one()
    transition(app_session, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 0, "x")
    after = app_session.execute(
        sa.text("SELECT updated_at FROM tasks WHERE id=:t"), {"t": seeded_task}
    ).scalar_one()
    assert after > before


def test_illegal_transition_raises_before_any_sql(app_session, seeded_task):
    with pytest.raises(IllegalTransition):
        transition(app_session, seeded_task, TaskState.QUEUED, TaskState.MERGED, 0, "x")
    assert not app_session.in_transaction()


def test_oversized_event_payload_raises_before_any_sql(app_session, seeded_task):
    with pytest.raises(ValueError, match="65536") as exc:
        transition(
            app_session,
            seeded_task,
            TaskState.QUEUED,
            TaskState.SCOPING,
            0,
            "x",
            {"blob": "s3cr3t" + "x" * 70_000},
        )
    assert "s3cr3t" not in str(exc.value)
    assert not app_session.in_transaction()
    row = app_session.execute(
        sa.text("SELECT state, version FROM tasks WHERE id=:t"), {"t": seeded_task}
    ).one()
    assert (row.state, row.version) == ("QUEUED", 0)


def test_stale_version_raises_and_writes_nothing(app_session, seeded_task):
    n_before = app_session.execute(sa.text("SELECT count(*) FROM events")).scalar_one()
    with pytest.raises(StaleTransition):
        transition(app_session, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 7, "x")
    app_session.rollback()
    row = app_session.execute(
        sa.text("SELECT state, version FROM tasks WHERE id=:t"), {"t": seeded_task}
    ).one()
    assert (row.state, row.version) == ("QUEUED", 0)
    assert app_session.execute(sa.text("SELECT count(*) FROM events")).scalar_one() == n_before


def test_wrong_from_state_is_stale(app_session, seeded_task):
    with pytest.raises(StaleTransition):
        transition(app_session, seeded_task, TaskState.SCOPING, TaskState.PLANNING, 0, "x")


def test_concurrent_writers_exactly_one_wins(app_engine, seeded_task):
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        try:
            with Session(app_engine) as s:
                barrier.wait(timeout=10)
                try:
                    transition(s, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 0, "race")
                    s.commit()
                    result = "ok"
                except StaleTransition:
                    s.rollback()
                    result = "stale"
        except BaseException as exc:
            result = f"error:{exc!r}"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == ["ok", "stale"]
    with Session(app_engine) as s:
        row = s.execute(
            sa.text("SELECT state, version FROM tasks WHERE id=:t"), {"t": seeded_task}
        ).one()
        n = s.execute(
            sa.text("SELECT count(*) FROM events WHERE task_id=:t"), {"t": seeded_task}
        ).scalar_one()
    assert (row.state, row.version, n) == ("SCOPING", 1, 1)


def test_needs_human_resumes_only_to_recorded_state(app_session, seeded_task):
    v = transition(app_session, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 0, "s")
    v = transition(app_session, seeded_task, TaskState.SCOPING, TaskState.NEEDS_HUMAN, v, "breach")
    assert (
        app_session.execute(
            sa.text("SELECT resume_state FROM tasks WHERE id=:t"), {"t": seeded_task}
        ).scalar()
        == "SCOPING"
    )
    with pytest.raises(IllegalTransition):
        transition(app_session, seeded_task, TaskState.NEEDS_HUMAN, TaskState.PLANNING, v, "resume")
    v = transition(app_session, seeded_task, TaskState.NEEDS_HUMAN, TaskState.SCOPING, v, "resume")
    assert (
        app_session.execute(
            sa.text("SELECT resume_state FROM tasks WHERE id=:t"), {"t": seeded_task}
        ).scalar()
        is None
    )


def test_needs_human_can_always_close(app_session, seeded_task):
    v = transition(app_session, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 0, "s")
    v = transition(app_session, seeded_task, TaskState.SCOPING, TaskState.NEEDS_HUMAN, v, "b")
    assert (
        transition(app_session, seeded_task, TaskState.NEEDS_HUMAN, TaskState.CLOSED, v, "c") == 3
    )


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(data=st.data())
def test_random_legal_walk_versions_and_events(app_engine, repo_id, data):
    with session_scope(app_engine) as s:
        tid = new_task(s, repo_id)
        state, version = TaskState.QUEUED, 0
        for step in range(1, 16):
            options = sorted(ALLOWED[state] - {TaskState.NEEDS_HUMAN})
            if not options:
                break
            nxt = data.draw(st.sampled_from(options))
            version = transition(s, tid, state, nxt, version, "walk")
            assert version == step
            state = nxt
        n = s.execute(
            sa.text("SELECT count(*) FROM events WHERE task_id=:t AND type='walk'"), {"t": tid}
        ).scalar_one()
        assert n == version


def test_needs_human_resume_with_stale_version_is_stale(app_session, seeded_task):
    v = transition(app_session, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 0, "s")
    v = transition(app_session, seeded_task, TaskState.SCOPING, TaskState.NEEDS_HUMAN, v, "b")
    with pytest.raises(StaleTransition):
        transition(app_session, seeded_task, TaskState.NEEDS_HUMAN, TaskState.SCOPING, v - 1, "r")
