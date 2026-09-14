"""Severity -> channel policy. Emit decides; delivery happens later from the outbox."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Severity

PUSH_SEVERITIES = frozenset({
    Severity.WARNING.value, Severity.ACTION_REQUIRED.value, Severity.CRITICAL.value, Severity.RESOLVED.value,
})


@dataclass(frozen=True)
class PushDecision:
    status: str  # "pending" | "none"
    quiet: bool = False


def decide_push(severity: str, *, thread_pushed_before: bool, last_push_at: datetime | None,
        now: datetime, repeat_minutes: int) -> PushDecision:
    """Info stays in the inbox. A resolution is only pushed for something the owner was told about.

    A superseding event on a thread that was pushed recently is still delivered so the phone
    shows the latest content, but marked quiet so it replaces the earlier notification silently.
    """
    if severity not in PUSH_SEVERITIES:
        return PushDecision("none")
    if severity == Severity.RESOLVED.value:
        return PushDecision("pending" if thread_pushed_before else "none")
    quiet = last_push_at is not None and (now - last_push_at) < timedelta(minutes=max(0, repeat_minutes))
    return PushDecision("pending", quiet)
