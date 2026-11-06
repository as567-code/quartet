"""Integration tests for the repository helpers (flush-only; callers own the transaction)."""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.orm import Session

from quartet.db.models import Artifact, Event, Handover, Repo, Task
from quartet.db.repository import (
    UntrustedInputError,
    UntrustedInputInvalid,
    UntrustedInputTooLarge,
    append_event,
    create_repo,
    create_task,
    record_handover,
    save_artifact,
)
from quartet.domain.artifacts import (
    ARTIFACT_KINDS,
    ConflictReport,
    ContextDigest,
    Finding,
    HandoverNote,
    PatchSummary,
    Plan,
    ReviewReport,
    TestReport,
)
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
    tid = create_task(
        app_session, repo_id, "manual", "r#9", "x" * 512 + "\x00" * 10, "y" * 65_536 + "\x00" * 5
    )
    task = app_session.get(Task, tid)
    assert task.title == "x" * 512 and task.body_untrusted == "y" * 65_536


def test_external_ref_nul_stripped_in_row_and_event(app_session, repo_id):
    tid = create_task(app_session, repo_id, "manual", "r\x00#13", "t", "b")
    assert app_session.get(Task, tid).external_ref == "r#13"
    payload = app_session.execute(
        sa.text("SELECT payload FROM events WHERE task_id=:t"), {"t": tid}
    ).scalar()
    assert payload["external_ref"] == "r#13"


def test_external_ref_cap(app_session, repo_id):
    create_task(app_session, repo_id, "manual", "r" * 512, "t", "b")
    with pytest.raises(UntrustedInputTooLarge, match="external_ref"):
        create_task(app_session, repo_id, "manual", "r" * 513, "t", "b")


@pytest.mark.parametrize("field", ["title", "body_untrusted", "external_ref"])
def test_lone_surrogate_rejected_without_echo(app_session, repo_id, field):
    args: dict[str, Any] = {"external_ref": "r", "title": "t", "body_untrusted": "b"}
    args[field] = "ok\ud800"
    with pytest.raises(UntrustedInputInvalid) as exc:
        create_task(app_session, repo_id, "manual", **args)
    assert field in str(exc.value) and "\ud800" not in str(exc.value)
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in str(exc.value))


def test_error_classes_share_base():
    assert issubclass(UntrustedInputTooLarge, UntrustedInputError)
    assert issubclass(UntrustedInputInvalid, UntrustedInputError)
    assert issubclass(UntrustedInputError, ValueError)


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


def _minimal(kind: str) -> BaseModel:
    ctor: dict[str, Callable[[], BaseModel]] = {
        "plan": lambda: Plan.model_validate(
            {
                "schema_version": 1,
                "summary": "s",
                "steps": [],
                "predicted_files": [],
                "acceptance_criteria": [],
                "test_strategy": "t",
                "risk_level": "low",
                "out_of_scope_reason": None,
            }
        ),
        "patch_summary": lambda: PatchSummary(
            schema_version=1, summary="s", files_changed=["a"], rationale="r", known_limitations=[]
        ),
        "test_report": lambda: TestReport(
            schema_version=1,
            passed=1,
            failed=0,
            errors=0,
            skipped=0,
            failing_tests=[],
            new_tests=[],
            suite_command=["pytest"],
            output_excerpt="ok",
            post_rebase=False,
        ),
        "review_report": lambda: ReviewReport(
            schema_version=1,
            verdict="approve",
            findings=[
                Finding(file="a.py", line=None, severity="nit", rationale="r", suggestion=None)
            ],
            injection_suspected=False,
        ),
        "handover_note": lambda: HandoverNote(
            schema_version=1,
            goal="g",
            attempted=[],
            current_state="c",
            evidence=[],
            suggested_next_step="n",
        ),
        "conflict_report": lambda: ConflictReport(
            schema_version=1,
            base_sha="a",
            onto_sha="b",
            conflicting_files=[],
            resolution_attempted=False,
        ),
    }
    return ctor[kind]()


def test_minimal_builders_cover_every_kind():
    for kind, cls in ARTIFACT_KINDS.items():
        assert type(_minimal(kind)) is cls


@pytest.mark.parametrize("kind", list(ARTIFACT_KINDS))
def test_save_artifact_round_trips_every_kind(app_session, seeded_task, kind):
    artifact = _minimal(kind)
    row = app_session.get(Artifact, save_artifact(app_session, seeded_task, artifact))
    assert row.kind == kind and row.schema_version == 1
    assert row.payload == artifact.model_dump(mode="json")
    assert type(artifact).model_validate(row.payload) == artifact


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
