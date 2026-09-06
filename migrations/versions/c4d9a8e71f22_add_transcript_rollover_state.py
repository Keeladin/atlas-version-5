"""Add transcript rollover state.

Revision ID: c4d9a8e71f22
Revises: b2a7f9c3e441
Create Date: 2026-09-06
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4d9a8e71f22"
down_revision: str | Sequence[str] | None = "b2a7f9c3e441"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("transcripts", sa.Column("kind", sa.String(32), nullable=False, server_default="owner"))
    op.create_index("ix_transcripts_kind", "transcripts", ["kind"])
    op.add_column("transcripts", sa.Column("context_summary", sa.Text(), nullable=True))
    op.add_column("transcripts", sa.Column("summarized_through_turn_id", postgresql.UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("transcripts", "summarized_through_turn_id")
    op.drop_index("ix_transcripts_kind", table_name="transcripts")
    op.drop_column("transcripts", "kind")
    op.drop_column("transcripts", "context_summary")
