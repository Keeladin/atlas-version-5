"""Add action execution start timestamp.

Revision ID: d31f6b0c4a77
Revises: c4d9a8e71f22
Create Date: 2026-09-06
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d31f6b0c4a77"
down_revision: str | Sequence[str] | None = "c4d9a8e71f22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("actions", sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_actions_execution_started_at", "actions", ["execution_started_at"])


def downgrade() -> None:
    op.drop_index("ix_actions_execution_started_at", table_name="actions")
    op.drop_column("actions", "execution_started_at")
