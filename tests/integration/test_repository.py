"""Integration tests for the repository helpers (flush-only; callers own the transaction)."""

from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.orm import Session

from quartet.db.models import Artifact, Event, Handover, Repo, Task
from quartet.db.repository import (
    UntrustedInputTooLarge,
    append_event,
    create_repo,
    create_task,
    record_handover,
    save_artifact,
)
from quartet.domain.artifacts import ContextDigest
from quartet.domain.states import Role, TaskState

pytestmark = pytest.mark.integration


def test_body_cap_counts_utf8_bytes(app_session, repo_id):
    ok = "é" * (65_536 // 2)  # 2 bytes each -> exactly 65,536 bytes, 32,768 chars
    create_task(app_session, repo_id, "manual", "r#1", "t", ok)
    with pytest.raises(UntrustedInputTooLarge):
        create_task(app_session, repo_id, "manual", "r#2", "t", ok + "é")


def test_title_cap(app_session, repo_id):
    create_task(app_session, repo_id, "manual", "r#5a", "x" * 512, "b")
    with pytest.raises(UntrustedInputTooLarge):
        create_task(app_session, repo_id, "manual", "r#5", "x" * 513, "b")


def test_title_cap_counts_bytes(app_session, repo_id):
    with pytest.raises(UntrustedInputTooLarge):
        create_task(app_session, repo_id, "manual", "r#7", "é" * 257, "b")


def test_cap_error_never_echoes_content(app_session, repo_id):
    secret = "s3cr3t-" + "x" * 600
    with pytest.raises(UntrustedInputTooLarge) as exc:
        create_task(app_session, repo_id, "manual", "r#8", secret, "b")
    assert "s3cr3t" not in str(exc.value)
    assert "title" in str(exc.value) and "512" in str(exc.value)


def test_cap_applies_after_nul_strip(app_session, repo_id):
    create_task(app_session, repo_id, "manual", "r#9", "x" * 512 + "\x00" * 10, "b")


def test_create_task_writes_event(app_session, repo_id):
    tid = create_task(app_session, repo_id, "manual", "r#3", "t", "b")
    assert (
        app_session.execute(
            sa.text("SELECT type FROM events WHERE task_id=:t"), {"t": tid}
        ).scalar()
        == "task_created"
    )


def test_create_task_defaults(app_session, repo_id):
    task = app_session.get(Task, create_task(app_session, repo_id, "manual", "r#10", "t", "b"))
    assert task.priority == 100 and task.budget_usd == Decimal("5.00")
    assert task.state is TaskState.QUEUED and task.version == 0


def test_helpers_do_not_commit(app_engine, repo_id):
    with Session(app_engine) as session:
        tid = create_task(session, repo_id, "manual", "r#11", "t", "b")
        session.rollback()
    with Session(app_engine) as session:
        assert session.get(Task, tid) is None


def test_committed_task_visible_from_other_session(app_engine, repo_id):
    with Session(app_engine) as session, session.begin():
        tid = create_task(session, repo_id, "manual", "r#12", "t", "b")
    with Session(app_engine) as session:
        assert session.get(Task, tid) is not None


def test_create_repo_stores_mirror_path_as_str(app_session):
    rid = create_repo(app_session, "acme", "thing", "trunk", Path("/var/mirrors/thing.git"))
    repo = app_session.get(Repo, rid)
    assert repo.mirror_path == "/var/mirrors/thing.git" and repo.default_branch == "trunk"


def test_append_event_returns_id(app_session, seeded_task):
    first = append_event(app_session, seeded_task, "note", {"a": 1})
    second = append_event(app_session, seeded_task, "note", {"a": 2})
    assert isinstance(first, int) and second > first
    assert app_session.get(Event, first).payload == {"a": 1}


def test_save_artifact_stores_payload_and_version(app_session, seeded_task, sample_plan):
    aid = save_artifact(app_session, seeded_task, sample_plan)
    row = app_session.get(Artifact, aid)
    assert row.kind == "plan" and row.schema_version == 1
    assert row.payload == sample_plan.model_dump(mode="json")


def test_handover_round_trip(app_session, seeded_task, sample_plan):
    aid = save_artifact(app_session, seeded_task, sample_plan)
    digest = ContextDigest(
        schema_version=1, goal="g", constraints=[], open_questions=[], artifact_ids=[str(aid)]
    )
    hid = record_handover(app_session, seeded_task, Role.PLANNER, Role.IMPLEMENTER, aid, digest, 0)
    row = app_session.get(Handover, hid)
    assert row.to_role is Role.IMPLEMENTER and row.context_digest["goal"] == "g"


def test_unknown_artifact_type_rejected(app_session, seeded_task):
    class NotAnArtifact(BaseModel):
        x: int

    with pytest.raises(TypeError):
        save_artifact(app_session, seeded_task, NotAnArtifact(x=1))


def test_context_digest_is_not_an_artifact(app_session, seeded_task):
    digest = ContextDigest(
        schema_version=1, goal="g", constraints=[], open_questions=[], artifact_ids=[]
    )
    with pytest.raises(TypeError):
        save_artifact(app_session, seeded_task, digest)


def test_artifact_subclass_rejected_by_exact_type_match(app_session, seeded_task, sample_plan):
    class SubPlan(type(sample_plan)):  # type: ignore[misc]
        pass

    sub = SubPlan.model_validate(sample_plan.model_dump())
    with pytest.raises(TypeError):
        save_artifact(app_session, seeded_task, sub)


def test_untrusted_body_with_sql_metacharacters_is_stored_verbatim(app_session, repo_id):
    evil = "'); DROP TABLE tasks; -- {payload} %s $1"
    tid = create_task(app_session, repo_id, "manual", "r#4", "t", evil)
    assert app_session.get(Task, tid).body_untrusted == evil


def test_nul_bytes_are_stripped(app_session, repo_id):  # Postgres text cannot store NUL
    tid = create_task(app_session, repo_id, "manual", "r#6", "t\x00x", "a\x00b")
    t = app_session.get(Task, tid)
    assert (t.title, t.body_untrusted) == ("tx", "ab")
