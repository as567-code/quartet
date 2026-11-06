"""Initial schema: all PRD section 9.3 tables, claim index, grants, append-only events.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASK_STATES = (
    "QUEUED",
    "SCOPING",
    "REJECTED",
    "PLANNING",
    "PLAN_REVIEW",
    "WAITING_LEASES",
    "IMPLEMENTING",
    "TESTING",
    "REVIEWING",
    "INTEGRATING",
    "PR_DRAFT",
    "AWAITING_HUMAN",
    "MERGED",
    "CLOSED",
    "NEEDS_HUMAN",
)
ROLES = ("PLANNER", "IMPLEMENTER", "TESTER", "REVIEWER", "ORCHESTRATOR", "HUMAN")

_TABLES_DROP_ORDER = (
    "events",
    "pull_requests",
    "interventions",
    "checkpoints",
    "worktrees",
    "file_leases",
    "tool_calls",
    "llm_calls",
    "handovers",
    "role_steps",
    "artifacts",
    "tasks",
    "repos",
)

_NOW = sa.text("now()")
_EMPTY_JSON = sa.text("'{}'::jsonb")


def _state_enum() -> postgresql.ENUM:
    return postgresql.ENUM(*TASK_STATES, name="task_state", create_type=False)


def _role_enum() -> postgresql.ENUM:
    return postgresql.ENUM(*ROLES, name="role", create_type=False)


def _id() -> "sa.Column[Any]":
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _fk(name: str, target: str, *, nullable: bool = False) -> "sa.Column[Any]":
    return sa.Column(name, postgresql.UUID(as_uuid=True), sa.ForeignKey(target), nullable=nullable)


def _ts(name: str, *, default: bool = True) -> "sa.Column[Any]":
    if default:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=_NOW)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _in(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    listed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=name)


def _json(name: str) -> "sa.Column[Any]":
    return sa.Column(name, postgresql.JSONB, nullable=False, server_default=_EMPTY_JSON)


def _int(name: str, default: str = "0") -> "sa.Column[Any]":
    return sa.Column(name, sa.Integer, nullable=False, server_default=default)


def _str(name: str, *, nullable: bool = False, default: str | None = None) -> "sa.Column[Any]":
    return sa.Column(name, sa.Text, nullable=nullable, server_default=default)


def upgrade() -> None:
    postgresql.ENUM(*TASK_STATES, name="task_state").create(op.get_bind())
    postgresql.ENUM(*ROLES, name="role").create(op.get_bind())

    op.create_table(
        "repos",
        _id(),
        _str("owner"),
        _str("name"),
        _str("default_branch", default="main"),
        _str("mirror_path", nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("owner", "name", name="uq_repos_owner_name"),
    )

    op.create_table(
        "tasks",
        _id(),
        _fk("repo_id", "repos.id"),
        _str("source"),
        _str("external_ref", nullable=True),
        _str("title"),
        _str("body_untrusted", default=""),
        sa.Column("state", _state_enum(), nullable=False),
        sa.Column("resume_state", _state_enum(), nullable=True),
        _int("version"),
        _int("priority", "100"),
        _int("attempt"),
        _str("risk_level", nullable=True),
        _str("lease_owner", nullable=True),
        _ts("lease_expires_at", default=False),
        sa.Column("budget_usd", sa.Numeric(12, 4), nullable=False, server_default="5.0000"),
        sa.Column("spent_usd", sa.Numeric(12, 4), nullable=False, server_default="0"),
        _ts("created_at"),
        _ts("updated_at"),
        _in("source", ("github_issue", "suite", "manual"), "ck_tasks_source"),
        _in("risk_level", ("low", "medium", "high"), "ck_tasks_risk_level"),
        sa.CheckConstraint("octet_length(title) <= 512", name="ck_tasks_title_len"),
        sa.CheckConstraint("octet_length(body_untrusted) <= 65536", name="ck_tasks_body_len"),
    )

    op.create_table(
        "artifacts",
        _id(),
        _fk("task_id", "tasks.id"),
        _str("kind"),
        _int("schema_version", "1"),
        _json("payload"),
        _ts("created_at"),
        _in(
            "kind",
            (
                "plan",
                "patch_summary",
                "test_report",
                "review_report",
                "conflict_report",
                "handover_note",
            ),
            "ck_artifacts_kind",
        ),
    )

    op.create_table(
        "role_steps",
        _id(),
        _fk("task_id", "tasks.id"),
        sa.Column("role", _role_enum(), nullable=False),
        _int("round"),
        _int("attempt"),
        _str("status", default="running"),
        _str("checkpoint_sha", nullable=True),
        _fk("output_artifact_id", "artifacts.id", nullable=True),
        _str("error", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("task_id", "role", "round", "attempt", name="uq_role_steps_step"),
        _in("status", ("running", "done", "failed"), "ck_role_steps_status"),
    )

    op.create_table(
        "handovers",
        _id(),
        _fk("task_id", "tasks.id"),
        sa.Column("from_role", _role_enum(), nullable=False),
        sa.Column("to_role", _role_enum(), nullable=False),
        _fk("artifact_id", "artifacts.id", nullable=True),
        _json("context_digest"),
        _int("attempt"),
        _ts("created_at"),
    )

    op.create_table(
        "llm_calls",
        _id(),
        _fk("step_id", "role_steps.id"),
        _str("provider"),
        _str("model"),
        _int("tokens_in"),
        _int("tokens_out"),
        _int("cache_read_tokens"),
        _int("cache_write_tokens"),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        _str("stop_reason", nullable=True),
        _str("trace_id", nullable=True),
        _ts("created_at"),
    )

    op.create_table(
        "tool_calls",
        _id(),
        _fk("step_id", "role_steps.id"),
        _str("tool"),
        _json("args_redacted"),
        sa.Column("is_error", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        _ts("created_at"),
    )

    op.create_table(
        "file_leases",
        _id(),
        _fk("task_id", "tasks.id"),
        _fk("repo_id", "repos.id"),
        _str("path_glob"),
        _str("mode"),
        _ts("acquired_at"),
        _ts("released_at", default=False),
        _str("source"),
        _ts("created_at"),
        _in("mode", ("read", "write"), "ck_file_leases_mode"),
        _in("source", ("plan", "extension"), "ck_file_leases_source"),
    )

    op.create_table(
        "worktrees",
        _id(),
        _fk("task_id", "tasks.id"),
        _str("path"),
        _str("branch"),
        _str("base_sha"),
        _str("head_sha", nullable=True),
        _str("status", default="active"),
        _ts("created_at"),
        _ts("updated_at"),
    )

    op.create_table(
        "checkpoints",
        _id(),
        _fk("task_id", "tasks.id"),
        _str("kind"),
        _str("status", default="pending"),
        _str("decided_by", nullable=True),
        _ts("decided_at", default=False),
        _str("note", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _in("kind", ("plan", "merge", "security"), "ck_checkpoints_kind"),
        _in("status", ("pending", "approved", "rejected"), "ck_checkpoints_status"),
    )

    op.create_table(
        "interventions",
        _id(),
        _fk("task_id", "tasks.id"),
        _str("kind"),
        _str("actor"),
        _str("source"),
        _ts("created_at"),
        _in("source", ("cli", "auto"), "ck_interventions_source"),
    )

    op.create_table(
        "pull_requests",
        _id(),
        _fk("task_id", "tasks.id"),
        sa.Column("number", sa.Integer, nullable=False),
        _str("url"),
        _str("state"),
        _str("head_sha", nullable=True),
        sa.Column("draft", sa.Boolean, nullable=False, server_default=sa.true()),
        _ts("created_at"),
        _ts("updated_at"),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        _fk("task_id", "tasks.id"),
        _str("type"),
        _json("payload"),
        _ts("created_at"),
    )

    for table in (
        "artifacts",
        "role_steps",
        "handovers",
        "file_leases",
        "worktrees",
        "checkpoints",
        "interventions",
        "pull_requests",
        "events",
    ):
        op.create_index(f"ix_{table}_task_id", table, ["task_id"])
    op.create_index("ix_tasks_repo_id", "tasks", ["repo_id"])
    op.create_index("ix_llm_calls_step_id", "llm_calls", ["step_id"])
    op.create_index("ix_tool_calls_step_id", "tool_calls", ["step_id"])

    op.execute(
        "CREATE INDEX ix_tasks_claimable ON tasks (priority, created_at) "
        "WHERE lease_owner IS NULL AND state IN ('QUEUED','SCOPING','PLANNING','WAITING_LEASES',"
        "'IMPLEMENTING','TESTING','REVIEWING','INTEGRATING','PR_DRAFT')"
    )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO quartet_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO quartet_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE quartet_migrator IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quartet_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE quartet_migrator IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO quartet_app"
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON events FROM quartet_app")


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE quartet_migrator IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM quartet_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE quartet_migrator IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM quartet_app"
    )
    for table in _TABLES_DROP_ORDER:
        op.drop_table(table)
    op.execute("DROP TYPE role")
    op.execute("DROP TYPE task_state")
