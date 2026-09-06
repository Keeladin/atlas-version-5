from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class RunKind(StrEnum):
    FOREGROUND = "foreground"
    SCHEDULED = "scheduled"
    BACKGROUND = "background"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_FOR_OWNER = "waiting_for_owner"


class ActionStatus(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class Run(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    kind: RunKind
    status: RunStatus = RunStatus.RUNNING
    transcript_id: UUID | None = None
    workspace_id: UUID | None = None
    intent: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


class Action(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    operation: str
    target_hash: str
    status: ActionStatus = ActionStatus.PREPARED
    idempotency_key: str | None = None
    evidence: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
