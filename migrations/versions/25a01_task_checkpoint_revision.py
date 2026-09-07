"""Protect active_task_state updates with a database revision."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "25a01"
down_revision: str | Sequence[str] | None = "f4a7c91d2e30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("transcripts", sa.Column("active_task_revision", sa.BigInteger(), nullable=False, server_default="0"))
    op.execute("""UPDATE transcripts SET active_task_state = active_task_state ||
        jsonb_build_object('task_id', gen_random_uuid()::text, 'revision', 0, 'version', 2)
        WHERE active_task_state <> '{}'::jsonb""")


def downgrade() -> None:
    op.drop_column("transcripts", "active_task_revision")
