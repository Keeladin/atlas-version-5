"""add model-proposed memory candidate intake

Revision ID: 25a09
Revises: 25a08
Create Date: 2026-09-09

Candidates are non-authoritative model proposals. Runtime binds canonical
provenance and queues them for later memory-policy processing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "25a09"
down_revision: str | Sequence[str] | None = "25a08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_candidates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("durability", sa.String(length=32), nullable=False),
        sa.Column("proposed_action", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=160), nullable=True),
        sa.Column("namespace", sa.String(length=160), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_transcript_id", sa.UUID(), nullable=False),
        sa.Column("source_turn_id", sa.UUID(), nullable=False),
        sa.Column("source_provider_evidence_id", sa.UUID(), nullable=True),
        sa.Column(
            "decision_json", postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_transcript_id"], ["transcripts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_turn_id"], ["turns.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_provider_evidence_id"], ["turns.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "status", "kind", "scope", "durability", "subject", "namespace",
        "fingerprint", "source_transcript_id", "source_turn_id",
        "source_provider_evidence_id",
    ):
        op.create_index(
            f"ix_memory_candidates_{column}", "memory_candidates", [column]
        )
    op.create_index(
        "uq_pending_memory_candidate_fingerprint",
        "memory_candidates",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("memory_candidates")
