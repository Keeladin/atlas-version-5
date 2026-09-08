"""Run-scoped context that must not be inferred from global selected UI state."""
from contextvars import ContextVar
from uuid import UUID

current_transcript_id: ContextVar[UUID | None] = ContextVar(
    "atlas_current_transcript_id", default=None
)
