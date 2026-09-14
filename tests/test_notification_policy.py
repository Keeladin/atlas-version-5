from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from atlas.notifications.models import NotificationEvent
from atlas.notifications.policy import decide_push
from atlas.notifications.service import _project
from atlas.persistence.models import NotificationRow

NOW = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize("severity,expected", [
    ("info", "none"), ("warning", "pending"), ("action_required", "pending"), ("critical", "pending"),
])
def test_severity_channel_table(severity, expected) -> None:
    assert decide_push(severity, thread_pushed_before=False, last_push_at=None, now=NOW, repeat_minutes=60).status == expected


def test_resolved_only_pushes_when_the_thread_was_pushed() -> None:
    assert decide_push("resolved", thread_pushed_before=False, last_push_at=None, now=NOW, repeat_minutes=60).status == "none"
    assert decide_push("resolved", thread_pushed_before=True, last_push_at=NOW, now=NOW, repeat_minutes=60).status == "pending"


def test_superseding_push_inside_repeat_window_is_quiet_but_still_delivered() -> None:
    inside = decide_push("action_required", thread_pushed_before=True, last_push_at=NOW - timedelta(minutes=17), now=NOW, repeat_minutes=60)
    outside = decide_push("action_required", thread_pushed_before=True, last_push_at=NOW - timedelta(minutes=61), now=NOW, repeat_minutes=60)
    assert inside == inside.__class__("pending", True)
    assert outside.status == "pending" and outside.quiet is False


def test_event_validation() -> None:
    with pytest.raises(ValueError):
        NotificationEvent(source="x", kind="k", severity="loud", title="t")
    with pytest.raises(ValueError):
        NotificationEvent(source="x", kind="k", severity="info", title="  ")


def test_model_projection_redacts_sensitive_fields_and_body() -> None:
    row = NotificationRow(id=uuid4(), source="runtime.rdc", kind="auth_required", severity="action_required",
        title="Desktop Commander needs a new device code", body="Enter AB12-CD34 at https://x/device/verify",
        detail={"code": "AB12-CD34", "url": "https://x/device/verify"}, sensitive_fields=["code", "body"],
        thread_key="rdc.auth:1", status="open", push_status="pending", created_at=NOW)
    owner = _project(row)
    model = _project(row, audience="model")
    assert owner["detail"]["code"] == "AB12-CD34" and "AB12-CD34" in owner["body"]
    assert model["detail"]["code"] == "[redacted]" and model["body"] == "[redacted]"
    assert model["detail"]["url"] == "https://x/device/verify"
    assert model["sensitive_fields"] == ["code", "body"] and model["read"] is False
