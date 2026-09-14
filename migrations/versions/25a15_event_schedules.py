"""event-driven schedules: notifications can wake a scheduled task

Revision ID: 25a15
Revises: 25a14
Create Date: 2026-09-13

Adds notifications.wake_claimed_at, set atomically when the scheduler has decided which
event tasks (schedule_kind = 'event') a notification wakes. Downgrade removes event tasks
because older code cannot compute their next run.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "25a15"
down_revision: str | Sequence[str] | None = "25a14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("wake_claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_notifications_wake_claimed_at", "notifications", ["wake_claimed_at"])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM scheduled_tasks WHERE schedule_kind = 'event'"))
    op.drop_index("ix_notifications_wake_claimed_at", table_name="notifications")
    op.drop_column("notifications", "wake_claimed_at")
