"""Add owner chat titles and activity timestamps.

Revision ID: 25a07
Revises: 25a06
Create Date: 2026-09-08

Owner chats continue to use canonical transcript rows. closed_at means the owner
chat is not currently selected; it is not a deletion marker.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "25a07"
down_revision: str | Sequence[str] | None = "25a06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("transcripts", sa.Column("title", sa.Text(), nullable=True))
    op.add_column(
        "transcripts",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.execute("""
        UPDATE transcripts
        SET updated_at = COALESCE(
            (SELECT max(turns.created_at) FROM turns WHERE turns.transcript_id = transcripts.id),
            transcripts.created_at,
            now()
        )
    """)
    op.execute("""
        UPDATE transcripts
        SET title = CASE
            WHEN kind = 'owner' AND closed_at IS NULL THEN 'Atlas'
            WHEN kind = 'owner' THEN 'Chat ' || to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI')
            ELSE NULL
        END
        WHERE title IS NULL
    """)
    op.create_index("ix_transcripts_updated_at", "transcripts", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_transcripts_updated_at", table_name="transcripts")
    op.drop_column("transcripts", "updated_at")
    op.drop_column("transcripts", "title")
