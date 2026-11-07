"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


# Privileges: default privileges (0001_initial) give quartet_app SELECT, INSERT, UPDATE and
# DELETE on every table this migration creates. An append-only (audit) table must REVOKE
# UPDATE, DELETE and TRUNCATE from quartet_app explicitly (and grant column-level INSERT if ids
# or timestamps must be DB-stamped), then be added to APPEND_ONLY in
# tests/integration/test_schema.py.


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
