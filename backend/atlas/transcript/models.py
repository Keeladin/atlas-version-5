from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Actor(StrEnum):
    OWNER = "owner"
    ATLAS = "atlas"
    TOOL = "tool"
    SYSTEM = "system"


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ArtifactRefBlock(BaseModel):
    type: Literal["artifact_ref"] = "artifact_ref"
    artifact_id: UUID
    filename: str | None = None
    provenance: dict[str, object] = Field(default_factory=dict)


class ToolObservationBlock(BaseModel):
    type: Literal["tool_observation"] = "tool_observation"
    action_id: UUID | None = None
    artifact_id: UUID | None = None
    operation: str | None = None
    phase: str | None = None
    summary: str | None = None
    detail: dict[str, object] = Field(default_factory=dict)
    provenance: dict[str, object] = Field(default_factory=dict)

ContentBlock = Annotated[
    TextBlock | ArtifactRefBlock | ToolObservationBlock,
    Field(discriminator="type"),
]


class Transcript(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    kind: str = "owner"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    closed_at: datetime | None = None
    context_summary: str | None = None
    summarized_through_turn_id: UUID | None = None
    active_task_state: dict[str, object] = Field(default_factory=dict)


class Turn(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    transcript_id: UUID
    sequence: int | None = None
    actor: Actor
    blocks: list[ContentBlock]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
