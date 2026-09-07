"""Durable schedule occurrences and heartbeat recovery; preserve active_task_state."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "25a03"
down_revision: str | Sequence[str] | None = "25a02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("runs", sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("trigger_snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.create_index("ix_runs_heartbeat_at", "runs", ["heartbeat_at"])
    op.create_index("ix_runs_schedule_id", "runs", ["schedule_id"])
    op.create_unique_constraint("uq_schedule_occurrence", "runs", ["schedule_id", "scheduled_for"])


def downgrade() -> None:
    op.drop_constraint("uq_schedule_occurrence", "runs", type_="unique")
    op.drop_index("ix_runs_schedule_id", "runs")
    op.drop_index("ix_runs_heartbeat_at", "runs")
    for column in ("trigger_snapshot", "scheduled_for", "schedule_id", "heartbeat_at"):
        op.drop_column("runs", column)
