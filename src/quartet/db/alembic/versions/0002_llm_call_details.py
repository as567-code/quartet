"""LLM call detail columns (incl. cost_estimated), money precision Numeric(14, 6), tool-call ids.

Revision ID: 0002
Revises: 0001

New columns on existing tables inherit the app role's table-level grants from 0001; no new
tables are created here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOKEN_COLUMNS = (
    "tokens_in",
    "tokens_out",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def upgrade() -> None:
    op.add_column(
        "llm_calls",
        sa.Column("requested_model", sa.Text, nullable=False, server_default=""),
    )
    op.alter_column("llm_calls", "requested_model", server_default=None)
    op.add_column("llm_calls", sa.Column("request_id", sa.Text, nullable=True))
    op.add_column(
        "llm_calls",
        sa.Column("reasoning_tokens", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "llm_calls",
        sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
    )
    op.add_column(
        "llm_calls",
        sa.Column("cost_estimated", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.alter_column(
        "llm_calls",
        "cost_usd",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Numeric(14, 6),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.create_check_constraint(
        "ck_llm_calls_provider", "llm_calls", "provider IN ('anthropic', 'openai')"
    )
    op.create_check_constraint(
        "ck_llm_calls_tokens_nonneg",
        "llm_calls",
        " AND ".join(f"{c} >= 0" for c in _TOKEN_COLUMNS),
    )
    op.create_check_constraint("ck_llm_calls_cost_nonneg", "llm_calls", "cost_usd >= 0")

    op.alter_column(
        "tasks",
        "budget_usd",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Numeric(14, 6),
        existing_nullable=False,
        server_default="5.000000",
    )
    op.alter_column(
        "tasks",
        "spent_usd",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Numeric(14, 6),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )

    op.add_column("tool_calls", sa.Column("call_id", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("tool_calls", "call_id")

    op.alter_column(
        "tasks",
        "spent_usd",
        existing_type=sa.Numeric(14, 6),
        type_=sa.Numeric(12, 4),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "tasks",
        "budget_usd",
        existing_type=sa.Numeric(14, 6),
        type_=sa.Numeric(12, 4),
        existing_nullable=False,
        server_default="5.0000",
    )

    op.drop_constraint("ck_llm_calls_cost_nonneg", "llm_calls", type_="check")
    op.drop_constraint("ck_llm_calls_tokens_nonneg", "llm_calls", type_="check")
    op.drop_constraint("ck_llm_calls_provider", "llm_calls", type_="check")
    op.alter_column(
        "llm_calls",
        "cost_usd",
        existing_type=sa.Numeric(14, 6),
        type_=sa.Numeric(12, 4),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.drop_column("llm_calls", "cost_estimated")
    op.drop_column("llm_calls", "attempts")
    op.drop_column("llm_calls", "reasoning_tokens")
    op.drop_column("llm_calls", "request_id")
    op.drop_column("llm_calls", "requested_model")
