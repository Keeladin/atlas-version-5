from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ArtifactKind(StrEnum):
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    TOOL_RESULT = "tool_result"


class Artifact(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    kind: ArtifactKind
    filename: str | None = None
    media_type: str
    storage_key: str
    sha256: str
    size_bytes: int
    source: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
