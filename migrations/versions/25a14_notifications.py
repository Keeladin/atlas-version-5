"""add owner notifications, push subscriptions, and host monitor state

Revision ID: 25a14
Revises: 25a13
Create Date: 2026-09-13

Owner awareness ledger (notifications) with an outbox column for push delivery,
per-device Web Push subscriptions, and durable cursor/state for deterministic
runtime host monitors (first consumer: the Desktop Commander connector monitor).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "25a14"
down_revision: str | Sequence[str] | None = "25a13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_object() -> sa.TextClause:
    return sa.text("'{}'::jsonb")


def _json_array() -> sa.TextClause:
    return sa.text("'[]'::jsonb")


NOTIFICATION_INDEXES = ("source", "kind", "severity", "thread_key", "status", "created_at", "run_id", "push_status")


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("detail", postgresql.JSONB(), server_default=_json_object(), nullable=False),
        sa.Column("sensitive_fields", postgresql.JSONB(), server_default=_json_array(), nullable=False),
        sa.Column("thread_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("push_status", sa.String(length=16), server_default="none", nullable=False),
        sa.Column("push_quiet", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("push_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("push_result", postgresql.JSONB(), server_default=_json_object(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in NOTIFICATION_INDEXES:
        op.create_index(f"ix_notifications_{column}", "notifications", [column])

    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint", name="uq_push_subscriptions_endpoint"),
    )
    op.create_index("ix_push_subscriptions_disabled_at", "push_subscriptions", ["disabled_at"])

    op.create_table(
        "host_monitor_state",
        sa.Column("monitor_id", sa.String(length=64), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("state", postgresql.JSONB(), server_default=_json_object(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("monitor_id"),
    )


def downgrade() -> None:
    op.drop_table("host_monitor_state")
    op.drop_index("ix_push_subscriptions_disabled_at", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
    for column in NOTIFICATION_INDEXES:
        op.drop_index(f"ix_notifications_{column}", table_name="notifications")
    op.drop_table("notifications")
