from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ACTION_REQUIRED = "action_required"
    CRITICAL = "critical"
    RESOLVED = "resolved"


SEVERITIES = frozenset(item.value for item in Severity)
MODEL_SEVERITIES = frozenset({Severity.INFO.value, Severity.WARNING.value})
REDACTED = "[redacted]"


@dataclass(frozen=True)
class NotificationEvent:
    """One structured owner-facing event. Titles must never carry secrets."""

    source: str
    kind: str
    severity: str
    title: str
    body: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    sensitive_fields: tuple[str, ...] = ()
    thread_key: str | None = None
    run_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"Unknown notification severity: {self.severity}")
        if not self.title.strip():
            raise ValueError("Notification title is required")
