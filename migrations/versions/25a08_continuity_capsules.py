"""Add rebuildable cross-chat continuity capsules.

Revision ID: 25a08
Revises: 25a07
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "25a08"
down_revision: str | Sequence[str] | None = "25a07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "continuity_capsules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transcript_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("start_sequence", sa.BigInteger(), nullable=False),
        sa.Column("end_sequence", sa.BigInteger(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["transcript_id"], ["transcripts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transcript_id", "revision", name="uq_continuity_capsule_revision"),
    )
    op.create_index("ix_continuity_capsules_transcript_id", "continuity_capsules", ["transcript_id"])
    op.create_index(
        "ix_continuity_capsules_transcript_created",
        "continuity_capsules",
        ["transcript_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_continuity_capsules_transcript_created", table_name="continuity_capsules")
    op.drop_index("ix_continuity_capsules_transcript_id", table_name="continuity_capsules")
    op.drop_table("continuity_capsules")
