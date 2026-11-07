"""ORM insert helpers for repos, tasks, artifacts, handovers and events.

Transaction ownership (ruling R29): these helpers never commit. They ``flush()`` to obtain
generated ids and return them; the caller owns the transaction, exactly as with
``transition()``. ``create_task`` writes its ``task_created`` event in that same uncommitted
transaction, so the task and its event commit or roll back together.

Untrusted text (``title``, ``body_untrusted``, ``external_ref``) has NUL characters stripped
first (Postgres ``text`` cannot store them), is checked for UTF-8 encodability (lone surrogates
raise ``UntrustedInputInvalid``) and is capped by UTF-8 *byte* length. The database CHECK
constraints remain as defence in depth.
"""

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.orm import Session

from quartet.db.models import TASK_SOURCES, Artifact, Event, Handover, Repo, Task
from quartet.domain.artifacts import ARTIFACT_KINDS, ContextDigest
from quartet.domain.events import check_event_payload
from quartet.domain.states import Role, TaskState

TITLE_MAX_BYTES = 512
BODY_MAX_BYTES = 65_536
EXTERNAL_REF_MAX_BYTES = 512
ARTIFACT_MAX_BYTES = 262_144
TASK_ARTIFACTS_MAX_BYTES = 4_194_304


class UntrustedInputError(ValueError):
    """Base class: an untrusted field was rejected. Messages never echo the content."""


class UntrustedInputTooLarge(UntrustedInputError):
    """An untrusted field exceeds its byte cap."""


class UntrustedInputInvalid(UntrustedInputError):
    """An untrusted field cannot be encoded as UTF-8 (for example a lone surrogate)."""


class ArtifactTooLarge(ValueError):
    """An artifact, or a task's artifacts in total, exceed their byte cap. Sizes only."""


def _clean(field: str, value: str, limit: int) -> str:
    """NUL-strip, validate UTF-8 encodability, then cap by byte length."""
    cleaned = value.replace("\x00", "")
    try:
        size = len(cleaned.encode("utf-8"))
    except UnicodeEncodeError:
        raise UntrustedInputInvalid(f"{field} is not valid UTF-8 text") from None
    if size > limit:
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
    external_ref: str | None,
    title: str,
    body_untrusted: str,
    priority: int = 100,
    budget_usd: Decimal = Decimal("5.00"),
) -> UUID:
    """Insert a QUEUED task and its ``task_created`` event (``external_ref=None`` -> NULL)."""
    if source not in TASK_SOURCES:
        raise ValueError("source must be one of: " + ", ".join(TASK_SOURCES))
    if external_ref is not None:
        external_ref = _clean("external_ref", external_ref, EXTERNAL_REF_MAX_BYTES)
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


def append_event(
    session: Session, task_id: UUID | None, type_: str, payload: Mapping[str, Any]
) -> int:
    """Insert an audit event (``task_id=None`` for task-less events). ``id`` and ``created_at``
    are stamped by the database. Raises ``ValueError`` if the payload exceeds the size cap."""
    event = Event(task_id=task_id, type=type_, payload=check_event_payload(payload))
    session.add(event)
    session.flush()
    return event.id


def save_artifact(session: Session, task_id: UUID, artifact: BaseModel) -> UUID:
    kind = next((k for k, cls in ARTIFACT_KINDS.items() if type(artifact) is cls), None)
    if kind is None:
        raise TypeError(f"{type(artifact).__name__} is not a registered artifact kind")
    payload = artifact.model_dump(mode="json")
    size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > ARTIFACT_MAX_BYTES:
        raise ArtifactTooLarge(f"artifact is {size} bytes; the limit is {ARTIFACT_MAX_BYTES}")
    stored = session.execute(
        sa.text(
            "SELECT coalesce(sum(octet_length(payload::text)), 0) FROM artifacts WHERE task_id = :t"
        ),
        {"t": task_id},
    ).scalar_one()
    if int(stored) + size > TASK_ARTIFACTS_MAX_BYTES:
        raise ArtifactTooLarge(
            f"task artifacts would total {int(stored) + size} bytes; "
            f"the limit is {TASK_ARTIFACTS_MAX_BYTES}"
        )
    row = Artifact(
        task_id=task_id,
        kind=kind,
        schema_version=cast(Any, artifact).schema_version,
        payload=payload,
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
