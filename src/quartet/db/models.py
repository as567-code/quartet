"""ORM models for every PRD §9.3 table.

The migration ``0001_initial`` is hand-written; ``tests/integration/test_schema.py`` checks that
these models and the migrated schema agree on table and column names.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from quartet.domain.states import Role, TaskState


class Base(DeclarativeBase):
    """Declarative base for all Quartet tables (``str`` columns are TEXT, as in the migration)."""

    type_annotation_map = {str: sa.Text}  # noqa: RUF012  (SQLAlchemy's documented hook)


def _pg_enum(enum: type[PyEnum], name: str) -> sa.Enum:
    return sa.Enum(enum, name=name, values_callable=lambda e: [m.value for m in e])


def _in(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    listed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=name)


_NOW = sa.text("now()")
_EMPTY_JSON = sa.text("'{}'::jsonb")

TASK_SOURCES = ("github_issue", "suite", "manual")
RISK_LEVELS = ("low", "medium", "high")
STEP_STATUSES = ("running", "done", "failed")
ARTIFACT_KINDS = (
    "plan",
    "patch_summary",
    "test_report",
    "review_report",
    "conflict_report",
    "handover_note",
)
LEASE_MODES = ("read", "write")
LEASE_SOURCES = ("plan", "extension")
CHECKPOINT_KINDS = ("plan", "merge", "security")
CHECKPOINT_STATUSES = ("pending", "approved", "rejected")
INTERVENTION_SOURCES = ("cli", "auto")


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _created() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), server_default=_NOW)


def _updated() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), server_default=_NOW, onupdate=sa.func.now())


class Repo(Base):
    __tablename__ = "repos"
    __table_args__ = (sa.UniqueConstraint("owner", "name", name="uq_repos_owner_name"),)

    id: Mapped[uuid.UUID] = _pk()
    owner: Mapped[str]
    name: Mapped[str]
    default_branch: Mapped[str] = mapped_column(server_default="main")
    mirror_path: Mapped[str | None]
    created_at: Mapped[datetime] = _created()


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        _in("source", TASK_SOURCES, "ck_tasks_source"),
        _in("risk_level", RISK_LEVELS, "ck_tasks_risk_level"),
        sa.CheckConstraint("octet_length(title) <= 512", name="ck_tasks_title_len"),
        sa.CheckConstraint("octet_length(body_untrusted) <= 65536", name="ck_tasks_body_len"),
        sa.Index(
            "ix_tasks_claimable",
            "priority",
            "created_at",
            postgresql_where=sa.text(
                "lease_owner IS NULL AND state IN ('QUEUED','SCOPING','PLANNING','WAITING_LEASES',"
                "'IMPLEMENTING','TESTING','REVIEWING','INTEGRATING','PR_DRAFT')"
            ),
        ),
        sa.Index(
            "ix_tasks_leased",
            "lease_expires_at",
            postgresql_where=sa.text("lease_owner IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("repos.id"), index=True)
    source: Mapped[str]
    external_ref: Mapped[str | None]
    title: Mapped[str]
    body_untrusted: Mapped[str] = mapped_column(server_default="")
    state: Mapped[TaskState] = mapped_column(_pg_enum(TaskState, "task_state"))
    resume_state: Mapped[TaskState | None] = mapped_column(_pg_enum(TaskState, "task_state"))
    version: Mapped[int] = mapped_column(server_default="0")
    priority: Mapped[int] = mapped_column(server_default="100")
    attempt: Mapped[int] = mapped_column(server_default="0")
    risk_level: Mapped[str | None]
    lease_owner: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    budget_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), server_default="5.0000")
    spent_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), server_default="0")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (_in("kind", ARTIFACT_KINDS, "ck_artifacts_kind"),)

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tasks.id"), index=True)
    kind: Mapped[str]
    schema_version: Mapped[int] = mapped_column(server_default="1")
    payload: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, server_default=_EMPTY_JSON)
    created_at: Mapped[datetime] = _created()


class RoleStep(Base):
    __tablename__ = "role_steps"
    __table_args__ = (
        sa.UniqueConstraint("task_id", "role", "round", "attempt", name="uq_role_steps_step"),
        _in("status", STEP_STATUSES, "ck_role_steps_status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tasks.id"), index=True)
    role: Mapped[Role] = mapped_column(_pg_enum(Role, "role"))
    round: Mapped[int] = mapped_column(server_default="0")
    attempt: Mapped[int] = mapped_column(server_default="0")
    status: Mapped[str] = mapped_column(server_default="running")
    checkpoint_sha: Mapped[str | None]
    output_artifact_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("artifacts.id"))
    error: Mapped[str | None]
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Handover(Base):
    __tablename__ = "handovers"

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tasks.id"), index=True)
    from_role: Mapped[Role] = mapped_column(_pg_enum(Role, "role"))
    to_role: Mapped[Role] = mapped_column(_pg_enum(Role, "role"))
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("artifacts.id"))
    context_digest: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, server_default=_EMPTY_JSON
    )
    attempt: Mapped[int] = mapped_column(server_default="0")
    created_at: Mapped[datetime] = _created()


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = _pk()
    step_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("role_steps.id"), index=True)
    provider: Mapped[str]
    model: Mapped[str]
    tokens_in: Mapped[int] = mapped_column(server_default="0")
    tokens_out: Mapped[int] = mapped_column(server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), server_default="0")
    latency_ms: Mapped[int | None]
    stop_reason: Mapped[str | None]
    trace_id: Mapped[str | None]
    created_at: Mapped[datetime] = _created()


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = _pk()
    step_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("role_steps.id"), index=True)
    tool: Mapped[str]
    args_redacted: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, server_default=_EMPTY_JSON
    )
    is_error: Mapped[bool] = mapped_column(server_default=sa.false())
    duration_ms: Mapped[int | None]
    created_at: Mapped[datetime] = _created()


class FileLease(Base):
    __tablename__ = "file_leases"
    __table_args__ = (
        _in("mode", LEASE_MODES, "ck_file_leases_mode"),
        _in("source", LEASE_SOURCES, "ck_file_leases_source"),
    )

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tasks.id"), index=True)
    repo_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("repos.id"))
    path_glob: Mapped[str]
    mode: Mapped[str]
    acquired_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=_NOW)
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    source: Mapped[str]
    created_at: Mapped[datetime] = _created()


class Worktree(Base):
    __tablename__ = "worktrees"

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tasks.id"), index=True)
    path: Mapped[str]
    branch: Mapped[str]
    base_sha: Mapped[str]
    head_sha: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="active")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Checkpoint(Base):
    __tablename__ = "checkpoints"
    __table_args__ = (
        _in("kind", CHECKPOINT_KINDS, "ck_checkpoints_kind"),
        _in("status", CHECKPOINT_STATUSES, "ck_checkpoints_status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tasks.id"), index=True)
    kind: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="pending")
    decided_by: Mapped[str | None]
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    note: Mapped[str | None]
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Intervention(Base):
    __tablename__ = "interventions"
    __table_args__ = (_in("source", INTERVENTION_SOURCES, "ck_interventions_source"),)

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tasks.id"), index=True)
    kind: Mapped[str]
    actor: Mapped[str]
    source: Mapped[str]
    created_at: Mapped[datetime] = _created()


class PullRequest(Base):
    __tablename__ = "pull_requests"

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tasks.id"), index=True)
    number: Mapped[int]
    url: Mapped[str]
    state: Mapped[str]
    head_sha: Mapped[str | None]
    draft: Mapped[bool] = mapped_column(server_default=sa.true())
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    # Nullable: worker and kill-switch events (Phase 6) belong to no task.
    task_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("tasks.id"), index=True)
    type: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, server_default=_EMPTY_JSON)
    created_at: Mapped[datetime] = _created()
