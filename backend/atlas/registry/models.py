from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class CapabilityAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    AUTHENTICATION_REQUIRED = "authentication_required"


class CapabilitySource(StrEnum):
    MODEL = "model"
    PROVIDER = "provider"
    MCP = "mcp"
    SERVICE = "service"
    LOCAL_SOFTWARE = "local_software"
    ATLAS = "atlas"


class CapabilityEntry(BaseModel):
    id: str
    family: str
    description: str
    source: CapabilitySource
    provisioned: bool = True
    enabled: bool = False
    availability: CapabilityAvailability = CapabilityAvailability.UNAVAILABLE
    executable_operations: list[str] = Field(default_factory=list)
    trust: Literal["internal", "external"] = "internal"
