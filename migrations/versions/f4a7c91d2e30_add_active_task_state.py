"""Add durable active-task checkpoint state.

Revision ID: f4a7c91d2e30
Revises: d31f6b0c4a77
Create Date: 2026-09-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4a7c91d2e30"
down_revision: str | Sequence[str] | None = "d31f6b0c4a77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transcripts",
        sa.Column("active_task_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("transcripts", "active_task_state")
