from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EffectKind(StrEnum):
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    EXECUTE = "execute"


class AuthorityMode(StrEnum):
    AUTO = "auto"
    APPROVAL_REQUIRED = "approval_required"
    FORBIDDEN = "forbidden"


class OperationDescriptor(BaseModel):
    id: str
    capability_id: str
    family: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    effect: EffectKind = EffectKind.READ
    authority: AuthorityMode = AuthorityMode.AUTO
    trust: str = "internal"


class CapabilityFailure(RuntimeError):
    """An executor's definite failure: the outcome is known, unlike an ambiguous transport error.

    phase "completed": the operation ran and reported failure; "before_dispatch": nothing was started.
    """

    def __init__(self, message: str, *, phase: str = "completed", output: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.phase = phase if phase in {"completed", "before_dispatch"} else "completed"
        self.output = dict(output or {})


class CapabilityCallResult(BaseModel):
    status: str
    operation_id: str
    output: Any | None = None
    proposal_id: str | None = None
    message: str | None = None
