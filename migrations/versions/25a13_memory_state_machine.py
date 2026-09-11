"""add explicit memory verification state machine

Revision ID: 25a13
Revises: 25a12
Create Date: 2026-09-11

Persists model-blind evidence readings, proposal comparison, memory-graph
reconciliation, policy outcomes, conflict obligations, and canonical evidence
references. Discovery dedupe is evidence-set based; semantic dedupe remains a
reconciliation concern.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "25a13"
down_revision: str | Sequence[str] | None = "25a12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_object() -> sa.TextClause:
    return sa.text("'{}'::jsonb")


def _json_array() -> sa.TextClause:
    return sa.text("'[]'::jsonb")


def upgrade() -> None:
    op.add_column(
        "durable_memories",
        sa.Column(
            "origin", sa.String(length=64),
            server_default="legacy_pre25a13", nullable=False,
        ),
    )
    op.create_index(
        "ix_durable_memories_origin",
        "durable_memories",
        ["origin"],
    )
    op.add_column(
        "durable_memories",
        sa.Column(
            "grounding_status", sa.String(length=32),
            server_default="legacy_unverified", nullable=False,
        ),
    )
    op.create_index(
        "ix_durable_memories_grounding_status",
        "durable_memories",
        ["grounding_status"],
    )
    # Every durable row that predates the evidence graph is unverified, regardless
    # of record_kind. Owner submission/import ownership must not bootstrap claim
    # authority. Only an explicit publication/review path may set verified later.
    op.execute(
        "UPDATE durable_memories "
        "SET grounding_status = 'legacy_unverified', origin = 'legacy_pre25a13'"
    )
    op.alter_column(
        "durable_memories",
        "origin",
        server_default="runtime_unclassified",
    )

    op.drop_index("uq_pending_memory_candidate_fingerprint", table_name="memory_candidates")
    op.add_column(
        "memory_candidates",
        sa.Column("evidence_set_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_memory_candidates_evidence_set_hash",
        "memory_candidates",
        ["evidence_set_hash"],
    )
    op.add_column(
        "memory_candidates",
        sa.Column("proposer_model", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_memory_candidates_proposer_model",
        "memory_candidates",
        ["proposer_model"],
    )
    op.add_column(
        "memory_candidates",
        sa.Column(
            "intake_path", sa.String(length=32), server_default="foreground", nullable=False
        ),
    )
    op.create_index(
        "ix_memory_candidates_intake_path",
        "memory_candidates",
        ["intake_path"],
    )
    op.add_column(
        "memory_candidates",
        sa.Column(
            "origin", sa.String(length=64), server_default="conversation", nullable=False
        ),
    )
    op.create_index(
        "ix_memory_candidates_origin",
        "memory_candidates",
        ["origin"],
    )
    op.add_column(
        "memory_candidates",
        sa.Column("temporal_horizon_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_memory_candidates_temporal_horizon_at",
        "memory_candidates",
        ["temporal_horizon_at"],
    )
    op.add_column(
        "memory_candidates",
        sa.Column("state_version", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "memory_reconciliation_attempts",
        sa.Column("candidate_state_version", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.create_index(
        "uq_reconcilable_memory_candidate_evidence",
        "memory_candidates",
        ["evidence_set_hash"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'leased', 'retained_short_term', 'awaiting_owner') "
            "AND evidence_set_hash IS NOT NULL"
        ),
    )

    op.drop_constraint(
        "ck_reconcilable_candidate_has_payload",
        "memory_candidates",
        type_="check",
    )
    op.create_check_constraint(
        "ck_reconcilable_candidate_has_payload",
        "memory_candidates",
        "status NOT IN ('pending', 'leased', 'retained_short_term', 'awaiting_owner') "
        "OR (content IS NOT NULL AND fingerprint IS NOT NULL)",
    )

    op.create_table(
        "memory_candidate_evidence",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("principal", sa.String(length=32), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("span_ref", sa.String(length=160), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["memory_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id", "turn_id", "span_ref",
            name="uq_memory_candidate_evidence_ref",
        ),
    )
    for column in ("candidate_id", "turn_id", "principal"):
        op.create_index(
            f"ix_memory_candidate_evidence_{column}",
            "memory_candidate_evidence",
            [column],
        )

    op.create_table(
        "memory_discovery_state",
        sa.Column("transcript_id", sa.UUID(), nullable=False),
        sa.Column("last_scanned_sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("source_revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["transcript_id"], ["transcripts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("transcript_id"),
    )

    op.create_table(
        "memory_independent_readings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("evidence_set_hash", sa.String(length=64), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "extracted_claims_json",
            postgresql.JSONB(),
            server_default=_json_array(),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.String(length=32), nullable=True),
        sa.Column("durability", sa.String(length=32), nullable=True),
        sa.Column("event_valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verifier_model", sa.String(length=128), nullable=True),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tombstone_operation_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["memory_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["memory_reconciliation_attempts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tombstone_operation_id"],
            ["shared_write_operations.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", name="uq_memory_independent_reading_attempt"),
    )
    for column in (
        "candidate_id", "attempt_id", "evidence_set_hash", "category", "scope",
        "durability", "event_valid_from", "event_valid_to", "tombstoned_at",
        "tombstone_operation_id",
    ):
        op.create_index(
            f"ix_memory_independent_readings_{column}",
            "memory_independent_readings",
            [column],
        )

    op.create_table(
        "memory_comparison_verdicts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("reading_id", sa.UUID(), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("normalized_content", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.String(length=32), nullable=True),
        sa.Column("durability", sa.String(length=32), nullable=True),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tombstone_operation_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["memory_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reading_id"], ["memory_independent_readings.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tombstone_operation_id"],
            ["shared_write_operations.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reading_id", name="uq_memory_comparison_reading"),
    )
    for column in (
        "candidate_id", "reading_id", "verdict", "category", "scope", "durability",
        "tombstoned_at", "tombstone_operation_id",
    ):
        op.create_index(
            f"ix_memory_comparison_verdicts_{column}",
            "memory_comparison_verdicts",
            [column],
        )

    op.create_table(
        "memory_reconciliation_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("comparison_id", sa.UUID(), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("target_memory_id", sa.UUID(), nullable=True),
        sa.Column("evaluated_memory_revision", sa.BigInteger(), nullable=False),
        sa.Column("replacement_content", sa.Text(), nullable=True),
        sa.Column("temporal_guard", sa.String(length=32), nullable=True),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tombstone_operation_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["memory_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["comparison_id"], ["memory_comparison_verdicts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_memory_id"], ["durable_memories.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["tombstone_operation_id"],
            ["shared_write_operations.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "candidate_id", "comparison_id", "relation", "target_memory_id",
        "evaluated_memory_revision", "temporal_guard", "tombstoned_at",
        "tombstone_operation_id",
    ):
        op.create_index(
            f"ix_memory_reconciliation_records_{column}",
            "memory_reconciliation_records",
            [column],
        )

    op.create_table(
        "memory_policy_decisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("reconciliation_id", sa.UUID(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["memory_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reconciliation_id"], ["memory_reconciliation_records.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reconciliation_id", name="uq_memory_policy_reconciliation"
        ),
    )
    for column in ("candidate_id", "reconciliation_id", "decision", "reason_code"):
        op.create_index(
            f"ix_memory_policy_decisions_{column}",
            "memory_policy_decisions",
            [column],
        )

    op.create_table(
        "memory_obligations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.UUID(), nullable=True),
        sa.Column(
            "origin", sa.String(length=64), server_default="runtime", nullable=False
        ),
        sa.Column("command_id", sa.UUID(), nullable=True),
        sa.Column("source_transcript_id", sa.UUID(), nullable=True),
        sa.Column("source_turn_id", sa.UUID(), nullable=True),
        sa.Column("resolution_source_transcript_id", sa.UUID(), nullable=True),
        sa.Column("resolution_source_turn_id", sa.UUID(), nullable=True),
        sa.Column("resolution_code", sa.String(length=64), nullable=True),
        sa.Column(
            "resolution_json",
            postgresql.JSONB(),
            server_default=_json_object(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["command_id"], ["memory_commands.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_transcript_id"], ["transcripts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_turn_id"], ["turns.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["resolution_source_transcript_id"], ["transcripts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["resolution_source_turn_id"], ["turns.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "kind", "status", "subject_type", "subject_id", "origin", "command_id",
        "source_transcript_id", "source_turn_id", "resolution_source_transcript_id",
        "resolution_source_turn_id", "resolution_code", "expires_at", "resolved_at",
    ):
        op.create_index(
            f"ix_memory_obligations_{column}", "memory_obligations", [column]
        )

    # Every active pre-25a13 memory becomes an explicit review obligation.
    # Migration ownership is not evidence; review confirmation later supplies
    # fresh canonical owner evidence or grounding can resolve it independently.
    op.execute(sa.text(
        "INSERT INTO memory_obligations "
        "(id, kind, status, subject_type, subject_id, origin, resolution_json) "
        "SELECT gen_random_uuid(), 'memory_review', 'pending', 'durable_memory', "
        "id, origin, jsonb_build_object('grounding_status', grounding_status) "
        "FROM durable_memories "
        "WHERE status = 'active' AND grounding_status = 'legacy_unverified'"
    ))

    op.create_table(
        "memory_conflicts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("reconciliation_id", sa.UUID(), nullable=False),
        sa.Column("target_memory_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("proposed_content", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tombstone_operation_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["memory_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reconciliation_id"], ["memory_reconciliation_records.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_memory_id"], ["durable_memories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tombstone_operation_id"],
            ["shared_write_operations.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "candidate_id", "reconciliation_id", "target_memory_id", "status",
        "reason_code", "tombstoned_at", "tombstone_operation_id",
    ):
        op.create_index(
            f"ix_memory_conflicts_{column}", "memory_conflicts", [column]
        )


def downgrade() -> None:
    op.drop_index(
        "ix_durable_memories_grounding_status", table_name="durable_memories"
    )
    op.drop_column("durable_memories", "grounding_status")
    op.drop_index("ix_durable_memories_origin", table_name="durable_memories")
    op.drop_column("durable_memories", "origin")
    op.drop_table("memory_discovery_state")

    for table, columns in (
        ("memory_conflicts", (
            "tombstone_operation_id", "tombstoned_at", "status", "target_memory_id",
            "reconciliation_id", "candidate_id",
        )),
        ("memory_obligations", (
            "resolved_at", "expires_at", "resolution_code",
            "resolution_source_turn_id", "resolution_source_transcript_id", "source_turn_id",
            "source_transcript_id", "command_id", "origin", "subject_id", "subject_type",
            "status", "kind",
        )),
        ("memory_policy_decisions", (
            "reason_code", "decision", "reconciliation_id", "candidate_id",
        )),
        ("memory_reconciliation_records", (
            "tombstone_operation_id", "tombstoned_at", "temporal_guard",
            "evaluated_memory_revision", "target_memory_id", "relation",
            "comparison_id", "candidate_id",
        )),
        ("memory_comparison_verdicts", (
            "tombstone_operation_id", "tombstoned_at", "durability", "scope",
            "category", "verdict", "reading_id", "candidate_id",
        )),
        ("memory_independent_readings", (
            "tombstone_operation_id", "tombstoned_at", "event_valid_to",
            "event_valid_from", "durability", "scope", "category",
            "evidence_set_hash", "attempt_id", "candidate_id",
        )),
        ("memory_candidate_evidence", ("principal", "turn_id", "candidate_id")),
    ):
        for column in columns:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)

    op.drop_constraint(
        "ck_reconcilable_candidate_has_payload",
        "memory_candidates",
        type_="check",
    )
    op.create_check_constraint(
        "ck_reconcilable_candidate_has_payload",
        "memory_candidates",
        "status NOT IN ('pending', 'leased', 'retained_short_term') "
        "OR (content IS NOT NULL AND fingerprint IS NOT NULL)",
    )

    op.drop_index(
        "uq_reconcilable_memory_candidate_evidence",
        table_name="memory_candidates",
    )
    op.drop_index(
        "ix_memory_candidates_evidence_set_hash",
        table_name="memory_candidates",
    )
    op.drop_index("ix_memory_candidates_temporal_horizon_at", table_name="memory_candidates")
    op.drop_column("memory_candidates", "temporal_horizon_at")
    op.drop_index("ix_memory_candidates_origin", table_name="memory_candidates")
    op.drop_column("memory_candidates", "origin")
    op.drop_index("ix_memory_candidates_intake_path", table_name="memory_candidates")
    op.drop_column("memory_candidates", "intake_path")
    op.drop_index("ix_memory_candidates_proposer_model", table_name="memory_candidates")
    op.drop_column("memory_candidates", "proposer_model")
    op.drop_column("memory_candidates", "evidence_set_hash")
    op.create_index(
        "uq_pending_memory_candidate_fingerprint",
        "memory_candidates",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'leased', 'retained_short_term') AND fingerprint IS NOT NULL"
        ),
    )
