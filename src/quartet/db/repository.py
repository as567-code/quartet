"""ORM insert helpers for repos, tasks, artifacts, handovers and events.

Transaction ownership (ruling R29): these helpers never commit. They ``flush()`` to obtain
generated ids and return them; the caller owns the transaction, exactly as with
``transition()``. ``create_task`` writes its ``task_created`` event in that same uncommitted
transaction, so the task and its event commit or roll back together.

Untrusted text (``title``, ``body_untrusted``) has NUL characters stripped first (Postgres
``text`` cannot store them) and is then capped by UTF-8 *byte* length. The database CHECK
constraints remain as defence in depth.
"""

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session

from quartet.db.models import Artifact, Event, Handover, Repo, Task
from quartet.domain.artifacts import ARTIFACT_KINDS, ContextDigest
from quartet.domain.states import Role, TaskState

TITLE_MAX_BYTES = 512
BODY_MAX_BYTES = 65_536


class UntrustedInputTooLarge(ValueError):
    """An untrusted field exceeds its byte cap. The message never echoes the content."""


def _clean(field: str, value: str, limit: int) -> str:
    cleaned = value.replace("\x00", "")
    if len(cleaned.encode("utf-8")) > limit:
        raise UntrustedInputTooLarge(f"{field} exceeds the {limit}-byte UTF-8 limit")
    return cleaned


def create_repo(
    session: Session, owner: str, name: str, default_branch: str, mirror_path: Path
) -> UUID:
    repo = Repo(owner=owner, name=name, default_branch=default_branch, mirror_path=str(mirror_path))
    session.add(repo)
    session.flush()
    return repo.id


def create_task(
    session: Session,
    repo_id: UUID,
    source: Literal["github_issue", "suite", "manual"],
    external_ref: str,
    title: str,
    body_untrusted: str,
    priority: int = 100,
    budget_usd: Decimal = Decimal("5.00"),
) -> UUID:
    title = _clean("title", title, TITLE_MAX_BYTES)
    body = _clean("body_untrusted", body_untrusted, BODY_MAX_BYTES)
    task = Task(
        repo_id=repo_id,
        source=source,
        external_ref=external_ref,
        title=title,
        body_untrusted=body,
        state=TaskState.QUEUED,
        version=0,
        priority=priority,
        budget_usd=budget_usd,
    )
    session.add(task)
    session.flush()
    append_event(session, task.id, "task_created", {"source": source, "external_ref": external_ref})
    return task.id


def append_event(session: Session, task_id: UUID, type_: str, payload: Mapping[str, Any]) -> int:
    event = Event(task_id=task_id, type=type_, payload=dict(payload))
    session.add(event)
    session.flush()
    return event.id


def save_artifact(session: Session, task_id: UUID, artifact: BaseModel) -> UUID:
    kind = next((k for k, cls in ARTIFACT_KINDS.items() if type(artifact) is cls), None)
    if kind is None:
        raise TypeError(f"{type(artifact).__name__} is not a registered artifact kind")
    row = Artifact(
        task_id=task_id,
        kind=kind,
        schema_version=getattr(artifact, "schema_version", 1),
        payload=artifact.model_dump(mode="json"),
    )
    session.add(row)
    session.flush()
    return row.id


def record_handover(
    session: Session,
    task_id: UUID,
    from_role: Role,
    to_role: Role,
    artifact_id: UUID,
    digest: ContextDigest,
    attempt: int,
) -> UUID:
    row = Handover(
        task_id=task_id,
        from_role=from_role,
        to_role=to_role,
        artifact_id=artifact_id,
        context_digest=digest.model_dump(mode="json"),
        attempt=attempt,
    )
    session.add(row)
    session.flush()
    return row.id
