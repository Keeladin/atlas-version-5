"""Add derived transcript memory index; preserve active_task_state.

Revision ID: 25a04
Revises: 25a03
Create Date: 2026-09-08
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "25a04"
down_revision: str | Sequence[str] | None = "25a03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transcript_index_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("transcript_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("index_version", sa.String(32), nullable=False),
        sa.Column("start_sequence", sa.BigInteger(), nullable=False),
        sa.Column("end_sequence", sa.BigInteger(), nullable=False),
        sa.Column("source_turn_ids", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb")),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_vector", postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', coalesce(content, ''))", persisted=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now()),
        sa.UniqueConstraint("transcript_id", "index_version", "start_sequence", "end_sequence",
            name="uq_transcript_index_chunk_source_range"),
    )
    op.create_index("ix_transcript_index_chunks_transcript_id", "transcript_index_chunks", ["transcript_id"])
    op.create_index("ix_transcript_index_chunks_search_vector", "transcript_index_chunks", ["search_vector"],
        postgresql_using="gin")

    op.create_table(
        "transcript_index_state",
        sa.Column("transcript_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transcripts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("index_version", sa.String(32), primary_key=True),
        sa.Column("last_indexed_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("transcript_index_state")
    op.drop_index("ix_transcript_index_chunks_search_vector", table_name="transcript_index_chunks")
    op.drop_index("ix_transcript_index_chunks_transcript_id", table_name="transcript_index_chunks")
    op.drop_table("transcript_index_chunks")
