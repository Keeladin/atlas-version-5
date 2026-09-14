"""Owner-attention rows also feed the awareness ledger.

Implemented as an ORM flush hook so every code path that creates or resolves an
OwnerAttentionRow (proposals, uncertain effects, interruptions, staged changes) is mirrored
without touching those write sites. The mirror is part of the same flush and transaction.
"""
from typing import Any
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session, attributes

from atlas.persistence.models import OwnerAttentionRow

from .models import NotificationEvent, Severity
from .service import emit_sync, resolve_thread_sync

_STATES = {
    "approval_required": (Severity.ACTION_REQUIRED.value, "approval_required"),
    "uncertain": (Severity.WARNING.value, "uncertain_action"),
    "interrupted": (Severity.WARNING.value, "interrupted"),
    "staged_change": (Severity.INFO.value, "staged_change"),
}
_DETAIL_KEYS = ("operation", "expires_at", "message", "download_url", "path", "external_id", "execution_started_at")
_EMIT_ON_CHANGE = ("state", "title", "detail")


def attention_thread_key(row: OwnerAttentionRow) -> str:
    if row.action_id is not None:
        return f"action:{row.action_id}"
    if row.state == "staged_change":
        return f"attention:{row.id}"
    return f"run:{row.run_id}:{row.state}"


def attention_event(row: OwnerAttentionRow) -> NotificationEvent | None:
    mapped = _STATES.get(row.state)
    if mapped is None:
        return None
    severity, kind = mapped
    source_detail: dict[str, Any] = dict(row.detail or {})
    detail = {key: source_detail[key] for key in _DETAIL_KEYS if key in source_detail}
    detail["attention_id"] = str(row.id)
    detail["open_url"] = "/"
    body = str(source_detail.get("message") or "")
    if row.state == "approval_required":
        body = body or "Waiting for your decision in Needs You."
    return NotificationEvent(source="authority", kind=kind, severity=severity, title=row.title, body=body,
        detail=detail, thread_key=attention_thread_key(row), run_id=row.run_id)


def _repeat_minutes() -> int:
    from atlas.config import get_settings

    return get_settings().push_repeat_minutes


def _changed(row: OwnerAttentionRow, attribute: str) -> bool:
    return attributes.get_history(row, attribute).has_changes()


def mirror_attention(session: Session, row: OwnerAttentionRow) -> None:
    if row.id is None:
        row.id = uuid4()
    if row.resolved:
        return
    event_ = attention_event(row)
    if event_ is not None:
        emit_sync(session, event_, repeat_minutes=_repeat_minutes())


def attention_flush_hook(session: Session, flush_context, instances) -> None:
    for row in list(session.new):
        if isinstance(row, OwnerAttentionRow):
            mirror_attention(session, row)
    for row in list(session.dirty):
        if not isinstance(row, OwnerAttentionRow) or row in session.deleted:
            continue
        if _changed(row, "resolved") and row.resolved:
            resolve_thread_sync(session, attention_thread_key(row))
        elif any(_changed(row, attribute) for attribute in _EMIT_ON_CHANGE):
            mirror_attention(session, row)


def register() -> None:
    if not event.contains(Session, "before_flush", attention_flush_hook):
        event.listen(Session, "before_flush", attention_flush_hook)


register()
