"""owner-set authority per operation

Revision ID: 25a16
Revises: 25a15
Create Date: 2026-09-13

The owner decides, per operation, whether Atlas may act automatically, must ask, or may not
act at all. A row here overrides the operation's default authority (from its descriptor, the
server's hints, or the MCP server configuration). No row means the default applies.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "25a16"
down_revision: str | Sequence[str] | None = "25a15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operation_authority",
        sa.Column("operation_id", sa.String(length=255), nullable=False),
        sa.Column("authority", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("operation_id"),
    )


def downgrade() -> None:
    op.drop_table("operation_authority")
