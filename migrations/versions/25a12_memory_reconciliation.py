"""add bounded memory reconciliation state and source revisions

Revision ID: 25a12
Revises: 25a11
Create Date: 2026-09-09

Adds candidate lease fencing, reconciliation-attempt history, and monotonic
canonical transcript content revisions used to fence index/capsule publication.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "25a12"
down_revision: str | Sequence[str] | None = "25a11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transcripts",
        sa.Column("content_revision", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.execute("UPDATE transcripts SET content_revision = next_turn_sequence")

    op.add_column(
        "transcript_index_chunks",
        sa.Column("source_revision", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.execute(
        "UPDATE transcript_index_chunks AS c "
        "SET source_revision = t.content_revision "
        "FROM transcripts AS t WHERE t.id = c.transcript_id"
    )

    op.add_column(
        "continuity_capsules",
        sa.Column("source_revision", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.execute(
        "UPDATE continuity_capsules AS c "
        "SET source_revision = t.content_revision "
        "FROM transcripts AS t WHERE t.id = c.transcript_id"
    )

    op.drop_constraint(
        "ck_pending_candidate_has_payload", "memory_candidates", type_="check"
    )
    op.drop_index(
        "uq_pending_memory_candidate_fingerprint", table_name="memory_candidates"
    )
    op.add_column("memory_candidates", sa.Column("lease_token", sa.UUID(), nullable=True))
    op.add_column(
        "memory_candidates",
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memory_candidates",
        sa.Column("attempt_count", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "memory_candidates",
        sa.Column("review_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memory_candidates",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ("lease_token", "leased_until", "review_after", "expires_at"):
        op.create_index(f"ix_memory_candidates_{column}", "memory_candidates", [column])
    op.create_index(
        "uq_pending_memory_candidate_fingerprint",
        "memory_candidates",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'leased', 'retained_short_term') AND fingerprint IS NOT NULL"
        ),
    )
    op.create_check_constraint(
        "ck_reconcilable_candidate_has_payload",
        "memory_candidates",
        "status NOT IN ('pending', 'leased', 'retained_short_term') "
        "OR (content IS NOT NULL AND fingerprint IS NOT NULL)",
    )

    op.create_table(
        "memory_reconciliation_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("lease_token", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.BigInteger(), nullable=False),
        sa.Column("evaluated_memory_revision", sa.BigInteger(), nullable=True),
        sa.Column("evaluated_source_revision", sa.BigInteger(), nullable=True),
        sa.Column("semantic_decision", sa.String(32), nullable=True),
        sa.Column("target_memory_id", sa.UUID(), nullable=True),
        sa.Column("operation_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "result_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["memory_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_memory_id"], ["durable_memories.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["shared_write_operations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id",
            "attempt_number",
            name="uq_memory_reconciliation_candidate_attempt",
        ),
    )
    for column in (
        "candidate_id",
        "lease_token",
        "semantic_decision",
        "target_memory_id",
        "operation_id",
        "status",
    ):
        op.create_index(
            f"ix_memory_reconciliation_attempts_{column}",
            "memory_reconciliation_attempts",
            [column],
        )


def downgrade() -> None:
    for column in (
        "status",
        "operation_id",
        "target_memory_id",
        "semantic_decision",
        "lease_token",
        "candidate_id",
    ):
        op.drop_index(
            f"ix_memory_reconciliation_attempts_{column}",
            table_name="memory_reconciliation_attempts",
        )
    op.drop_table("memory_reconciliation_attempts")

    op.drop_constraint(
        "ck_reconcilable_candidate_has_payload", "memory_candidates", type_="check"
    )
    op.drop_index(
        "uq_pending_memory_candidate_fingerprint", table_name="memory_candidates"
    )
    for column in ("expires_at", "review_after", "leased_until", "lease_token"):
        op.drop_index(f"ix_memory_candidates_{column}", table_name="memory_candidates")
    for column in ("expires_at", "review_after", "attempt_count", "leased_until", "lease_token"):
        op.drop_column("memory_candidates", column)
    op.create_index(
        "uq_pending_memory_candidate_fingerprint",
        "memory_candidates",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND fingerprint IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_pending_candidate_has_payload",
        "memory_candidates",
        "status <> 'pending' OR (content IS NOT NULL AND fingerprint IS NOT NULL)",
    )

    op.drop_column("continuity_capsules", "source_revision")
    op.drop_column("transcript_index_chunks", "source_revision")
    op.drop_column("transcripts", "content_revision")
