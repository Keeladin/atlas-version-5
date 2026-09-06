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


class ToolObservationBlock(BaseModel):
    type: Literal["tool_observation"] = "tool_observation"
    action_id: UUID | None = None
    artifact_id: UUID | None = None
    summary: str | None = None

ContentBlock = Annotated[
    TextBlock | ArtifactRefBlock | ToolObservationBlock,
    Field(discriminator="type"),
]


class Transcript(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    closed_at: datetime | None = None


class Turn(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    transcript_id: UUID
    actor: Actor
    blocks: list[ContentBlock]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
