"""add explicit memory lifecycle and deletion identity

Revision ID: 25a11
Revises: 25a10
Create Date: 2026-09-09

Active/superseded/retired records retain content. Deleted records retain only
stable identity and relationships; their content-bearing fields are null.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "25a11"
down_revision: str | Sequence[str] | None = "25a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("durable_memories", sa.Column("memory_kind", sa.String(32), nullable=True))
    op.add_column("durable_memories", sa.Column("scope", sa.String(32), server_default="cross_chat", nullable=False))
    op.add_column("durable_memories", sa.Column("scope_key", sa.String(255), nullable=True))
    op.add_column("durable_memories", sa.Column("durability", sa.String(32), server_default="long_term", nullable=False))
    op.add_column("durable_memories", sa.Column("subject", sa.String(160), nullable=True))
    op.add_column("durable_memories", sa.Column("namespace", sa.String(160), nullable=True))
    op.add_column("durable_memories", sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True))
    op.add_column("durable_memories", sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True))
    op.add_column("durable_memories", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("durable_memories", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("durable_memories", sa.Column("deletion_operation_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_durable_memories_deletion_operation",
        "durable_memories", "shared_write_operations",
        ["deletion_operation_id"], ["id"], ondelete="SET NULL",
    )
    op.execute("UPDATE durable_memories SET retired_at = forgotten_at WHERE status = 'forgotten'")
    op.execute("UPDATE durable_memories SET status = 'retired' WHERE status = 'forgotten'")
    op.drop_column("durable_memories", "forgotten_at")
    op.alter_column("durable_memories", "content", existing_type=sa.Text(), nullable=True)
    op.alter_column("durable_memories", "fingerprint", existing_type=sa.String(64), nullable=True)
    op.drop_index("uq_active_durable_memory_fingerprint", table_name="durable_memories")
    op.create_index(
        "uq_active_durable_memory_fingerprint", "durable_memories", ["fingerprint"], unique=True,
        postgresql_where=sa.text("status = 'active' AND fingerprint IS NOT NULL"),
    )
    for column in ("memory_kind", "scope", "scope_key", "durability", "subject", "namespace", "deletion_operation_id"):
        op.create_index(f"ix_durable_memories_{column}", "durable_memories", [column])
    op.create_check_constraint(
        "ck_deleted_memory_payload_shape", "durable_memories",
        "status <> 'deleted' OR (content IS NULL AND fingerprint IS NULL AND embedding IS NULL "
        "AND embedding_model IS NULL AND embedding_dimensions IS NULL AND embedded_at IS NULL "
        "AND deleted_at IS NOT NULL AND deletion_operation_id IS NOT NULL AND suppresses_recall IS TRUE)",
    )
    op.create_check_constraint(
        "ck_nondeleted_memory_has_payload", "durable_memories",
        "status = 'deleted' OR (content IS NOT NULL AND fingerprint IS NOT NULL)",
    )

    op.add_column("turns", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("turns", sa.Column("deletion_operation_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_turns_deletion_operation", "turns", "shared_write_operations",
        ["deletion_operation_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_turns_deletion_operation_id", "turns", ["deletion_operation_id"])

    op.add_column("memory_candidates", sa.Column("scope_key", sa.String(255), nullable=True))
    op.add_column("memory_candidates", sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memory_candidates", sa.Column("invalidation_operation_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_memory_candidates_invalidation_operation", "memory_candidates", "shared_write_operations",
        ["invalidation_operation_id"], ["id"], ondelete="SET NULL",
    )
    op.alter_column("memory_candidates", "content", existing_type=sa.Text(), nullable=True)
    op.alter_column("memory_candidates", "fingerprint", existing_type=sa.String(64), nullable=True)
    op.drop_index("uq_pending_memory_candidate_fingerprint", table_name="memory_candidates")
    op.create_index(
        "uq_pending_memory_candidate_fingerprint", "memory_candidates", ["fingerprint"], unique=True,
        postgresql_where=sa.text("status = 'pending' AND fingerprint IS NOT NULL"),
    )
    op.create_index("ix_memory_candidates_scope_key", "memory_candidates", ["scope_key"])
    op.create_index("ix_memory_candidates_invalidation_operation_id", "memory_candidates", ["invalidation_operation_id"])
    op.create_check_constraint(
        "ck_pending_candidate_has_payload", "memory_candidates",
        "status <> 'pending' OR (content IS NOT NULL AND fingerprint IS NOT NULL)",
    )

    op.create_table(
        "memory_provenance",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("memory_id", sa.UUID(), nullable=False),
        sa.Column("relationship", sa.String(32), nullable=False),
        sa.Column("source_turn_id", sa.UUID(), nullable=True),
        sa.Column("source_candidate_id", sa.UUID(), nullable=True),
        sa.Column("source_memory_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["durable_memories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_candidate_id"], ["memory_candidates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_memory_id"], ["durable_memories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "source_turn_id IS NOT NULL OR source_candidate_id IS NOT NULL OR source_memory_id IS NOT NULL",
            name="ck_memory_provenance_has_source",
        ),
    )
    for column in ("memory_id", "source_turn_id", "source_candidate_id", "source_memory_id", "relationship"):
        op.create_index(f"ix_memory_provenance_{column}", "memory_provenance", [column])

    op.create_table(
        "memory_deletion_receipts",
        sa.Column("operation_id", sa.UUID(), nullable=False),
        sa.Column("target_memory_id", sa.UUID(), nullable=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("affected_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["operation_id"], ["shared_write_operations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_memory_id"], ["durable_memories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_index("ix_memory_deletion_receipts_target_memory_id", "memory_deletion_receipts", ["target_memory_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_deletion_receipts_target_memory_id", table_name="memory_deletion_receipts")
    op.drop_table("memory_deletion_receipts")
    op.drop_table("memory_provenance")
    op.drop_constraint("ck_pending_candidate_has_payload", "memory_candidates", type_="check")
    op.drop_index("ix_memory_candidates_invalidation_operation_id", table_name="memory_candidates")
    op.drop_index("ix_memory_candidates_scope_key", table_name="memory_candidates")
    op.drop_index("uq_pending_memory_candidate_fingerprint", table_name="memory_candidates")
    op.alter_column("memory_candidates", "fingerprint", existing_type=sa.String(64), nullable=False)
    op.alter_column("memory_candidates", "content", existing_type=sa.Text(), nullable=False)
    op.drop_constraint("fk_memory_candidates_invalidation_operation", "memory_candidates", type_="foreignkey")
    op.drop_column("memory_candidates", "invalidation_operation_id")
    op.drop_column("memory_candidates", "invalidated_at")
    op.drop_column("memory_candidates", "scope_key")
    op.create_index(
        "uq_pending_memory_candidate_fingerprint", "memory_candidates", ["fingerprint"], unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index("ix_turns_deletion_operation_id", table_name="turns")
    op.drop_constraint("fk_turns_deletion_operation", "turns", type_="foreignkey")
    op.drop_column("turns", "deletion_operation_id")
    op.drop_column("turns", "deleted_at")
    op.drop_constraint("ck_nondeleted_memory_has_payload", "durable_memories", type_="check")
    op.drop_constraint("ck_deleted_memory_payload_shape", "durable_memories", type_="check")
    for column in ("deletion_operation_id", "namespace", "subject", "durability", "scope_key", "scope", "memory_kind"):
        op.drop_index(f"ix_durable_memories_{column}", table_name="durable_memories")
    op.drop_index("uq_active_durable_memory_fingerprint", table_name="durable_memories")
    op.alter_column("durable_memories", "fingerprint", existing_type=sa.String(64), nullable=False)
    op.alter_column("durable_memories", "content", existing_type=sa.Text(), nullable=False)
    op.add_column("durable_memories", sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE durable_memories SET forgotten_at = retired_at, status = 'forgotten' WHERE status = 'retired'")
    op.drop_constraint("fk_durable_memories_deletion_operation", "durable_memories", type_="foreignkey")
    for column in ("deletion_operation_id", "deleted_at", "retired_at", "valid_to", "valid_from", "namespace", "subject", "durability", "scope_key", "scope", "memory_kind"):
        op.drop_column("durable_memories", column)
    op.create_index(
        "uq_active_durable_memory_fingerprint", "durable_memories", ["fingerprint"], unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
