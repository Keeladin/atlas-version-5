"""add durable owner-directed memory semantics

Revision ID: 25a06
Revises: 25a05
Create Date: 2026-09-08

The canonical transcript and active_task_state remain untouched. This migration
adds canonical owner-directed memory records and an auditable command ledger.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "25a06"
down_revision: str | Sequence[str] | None = "25a05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "durable_memories",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("record_kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("suppresses_recall", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', coalesce(content, ''))", persisted=True),
            nullable=False,
        ),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("embedding_model", sa.String(length=128), nullable=True),
        sa.Column("embedding_dimensions", sa.BigInteger(), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_transcript_id", sa.UUID(), nullable=True),
        sa.Column("source_turn_id", sa.UUID(), nullable=True),
        sa.Column("supersedes_id", sa.UUID(), nullable=True),
        sa.Column("superseded_by_id", sa.UUID(), nullable=True),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_transcript_id"], ["transcripts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["durable_memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["durable_memories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_durable_memories_status", "durable_memories", ["status"])
    op.create_index("ix_durable_memories_record_kind", "durable_memories", ["record_kind"])
    op.create_index("ix_durable_memories_fingerprint", "durable_memories", ["fingerprint"])
    op.create_index("ix_durable_memories_suppresses_recall", "durable_memories", ["suppresses_recall"])
    op.create_index("ix_durable_memories_embedding_model", "durable_memories", ["embedding_model"])
    op.create_index("ix_durable_memories_source_transcript_id", "durable_memories", ["source_transcript_id"])
    op.create_index("ix_durable_memories_source_turn_id", "durable_memories", ["source_turn_id"])
    op.create_index("ix_durable_memories_supersedes_id", "durable_memories", ["supersedes_id"])
    op.create_index("ix_durable_memories_superseded_by_id", "durable_memories", ["superseded_by_id"])
    op.create_index(
        "uq_active_durable_memory_fingerprint",
        "durable_memories",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_durable_memories_search_vector",
        "durable_memories",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_durable_memories_embedding_cosine",
        "durable_memories",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )

    op.create_table(
        "memory_commands",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("arguments_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("source_transcript_id", sa.UUID(), nullable=True),
        sa.Column("source_turn_id", sa.UUID(), nullable=True),
        sa.Column("target_memory_id", sa.UUID(), nullable=True),
        sa.Column("replacement_memory_id", sa.UUID(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["source_transcript_id"], ["transcripts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["target_memory_id"], ["durable_memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["replacement_memory_id"], ["durable_memories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_commands_operation", "memory_commands", ["operation"])
    op.create_index("ix_memory_commands_status", "memory_commands", ["status"])
    op.create_index("ix_memory_commands_source_transcript_id", "memory_commands", ["source_transcript_id"])
    op.create_index("ix_memory_commands_source_turn_id", "memory_commands", ["source_turn_id"])
    op.create_index("ix_memory_commands_target_memory_id", "memory_commands", ["target_memory_id"])
    op.create_index("ix_memory_commands_replacement_memory_id", "memory_commands", ["replacement_memory_id"])


def downgrade() -> None:
    op.drop_table("memory_commands")
    op.drop_index("ix_durable_memories_embedding_cosine", table_name="durable_memories")
    op.drop_index("ix_durable_memories_search_vector", table_name="durable_memories")
    op.drop_index("uq_active_durable_memory_fingerprint", table_name="durable_memories")
    op.drop_table("durable_memories")
