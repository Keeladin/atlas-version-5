"""add shared-state write contract ledgers

Revision ID: 25a10
Revises: 25a09
Create Date: 2026-09-09

Shared mutable resources receive a runtime-owned version fence. Write operation
ids are durable idempotency identities with canonical provenance and outcomes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "25a10"
down_revision: str | Sequence[str] | None = "25a09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shared_resource_versions",
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("resource_type", "resource_id"),
    )
    op.create_table(
        "shared_write_operations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("expected_version", sa.BigInteger(), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("source_transcript_id", sa.UUID(), nullable=True),
        sa.Column("source_turn_id", sa.UUID(), nullable=True),
        sa.Column("source_run_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("observed_version", sa.BigInteger(), nullable=False),
        sa.Column("committed_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "result_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_transcript_id"], ["transcripts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_turn_id"], ["turns.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"], ["runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "resource_type",
        "resource_id",
        "operation",
        "actor",
        "source_transcript_id",
        "source_turn_id",
        "source_run_id",
        "status",
        "outcome",
    ):
        op.create_index(
            f"ix_shared_write_operations_{column}",
            "shared_write_operations",
            [column],
        )


def downgrade() -> None:
    op.drop_table("shared_write_operations")
    op.drop_table("shared_resource_versions")
