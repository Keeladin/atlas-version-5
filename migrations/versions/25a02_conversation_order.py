"""Sequence canonical turns and serialize foreground inference; preserve active_task_state."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "25a02"
down_revision: str | Sequence[str] | None = "25a01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("transcripts", sa.Column("next_turn_sequence", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("turns", sa.Column("sequence", sa.BigInteger(), nullable=True))
    # Historical causal order cannot be recovered; retain the old display order.
    op.execute("""WITH ordered AS (SELECT id, row_number() OVER
        (PARTITION BY transcript_id ORDER BY created_at, id) AS seq FROM turns)
        UPDATE turns SET sequence = ordered.seq FROM ordered WHERE turns.id = ordered.id""")
    op.execute("""UPDATE transcripts SET next_turn_sequence =
        COALESCE((SELECT max(sequence) FROM turns WHERE transcript_id = transcripts.id), 0)""")
    op.alter_column("turns", "sequence", nullable=False)
    op.create_unique_constraint("uq_transcript_turn_sequence", "turns", ["transcript_id", "sequence"])
    # Archive duplicate active transcripts without deleting any history/checkpoint.
    op.execute("""WITH owners AS (SELECT id, row_number() OVER (ORDER BY created_at DESC, id DESC) AS rank
        FROM transcripts WHERE kind = 'owner' AND closed_at IS NULL)
        UPDATE transcripts SET closed_at = now() FROM owners WHERE transcripts.id = owners.id AND owners.rank > 1""")
    op.create_index("uq_active_owner_transcript", "transcripts", ["kind"], unique=True,
        postgresql_where=sa.text("kind = 'owner' AND closed_at IS NULL"))
    op.add_column("runs", sa.Column("inference_active", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("runs", sa.Column("inference_status", sa.String(32), nullable=False, server_default="running"))
    op.execute("""UPDATE runs SET inference_status = CASE
        WHEN status = 'running' THEN 'running'
        WHEN status IN ('failed', 'cancelled') THEN 'failed'
        ELSE 'succeeded' END""")
    op.create_index("uq_foreground_inference", "runs", ["transcript_id"], unique=True,
        postgresql_where=sa.text("kind = 'foreground' AND inference_active"))


def downgrade() -> None:
    op.drop_index("uq_foreground_inference", "runs")
    op.drop_column("runs", "inference_status")
    op.drop_column("runs", "inference_active")
    op.drop_index("uq_active_owner_transcript", "transcripts")
    op.drop_constraint("uq_transcript_turn_sequence", "turns", type_="unique")
    op.drop_column("turns", "sequence")
    op.drop_column("transcripts", "next_turn_sequence")
