"""add semantic transcript memory vectors

Revision ID: 25a05
Revises: 25a04
Create Date: 2026-09-08

The canonical transcript and active_task_state remain untouched. This migration
adds only rebuildable semantic retrieval data to derived transcript chunks.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "25a05"
down_revision: str | Sequence[str] | None = "25a04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("transcript_index_chunks", sa.Column("embedding", Vector(1536), nullable=True))
    op.add_column("transcript_index_chunks", sa.Column("embedding_model", sa.String(length=128), nullable=True))
    op.add_column("transcript_index_chunks", sa.Column("embedding_dimensions", sa.BigInteger(), nullable=True))
    op.add_column("transcript_index_chunks", sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_transcript_index_chunks_embedding_model", "transcript_index_chunks", ["embedding_model"])
    op.create_index(
        "ix_transcript_index_chunks_embedding_cosine",
        "transcript_index_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_transcript_index_chunks_embedding_cosine", table_name="transcript_index_chunks")
    op.drop_index("ix_transcript_index_chunks_embedding_model", table_name="transcript_index_chunks")
    op.drop_column("transcript_index_chunks", "embedded_at")
    op.drop_column("transcript_index_chunks", "embedding_dimensions")
    op.drop_column("transcript_index_chunks", "embedding_model")
    op.drop_column("transcript_index_chunks", "embedding")
