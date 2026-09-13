"""Notification ledger: emit/thread semantics, the attention bridge, the outbox, and the RDC monitor pass."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from atlas.actions.authority import AuthorityStore
from atlas.actions.models import RunKind
from atlas.config import Settings
from atlas.monitors.rdc import RdcConfig, load_replay_entries, rdc_monitor_once
from atlas.notifications import NotificationEvent, NotificationService
from atlas.notifications.dispatcher import deliver_pending
from atlas.persistence.models import (
    HostMonitorStateRow,
    NotificationRow,
    OwnerAttentionRow,
    PushSubscriptionRow,
    RunRow,
    TranscriptRow,
)
from sqlalchemy import select

FIXTURE = Path(__file__).parent / "fixtures" / "rdc_journal_auth_flow.jsonl"


def _event(**overrides) -> NotificationEvent:
    base = {"source": "test", "kind": "k", "severity": "warning", "title": "Title", "body": "body", "thread_key": "t"}
    base.update(overrides)
    return NotificationEvent(**base)


async def _rows(session, thread: str) -> list[NotificationRow]:
    return list((await session.execute(select(NotificationRow).where(NotificationRow.thread_key == thread)
        .order_by(NotificationRow.created_at))).scalars().all())


@pytest.mark.asyncio
async def test_emit_dedupes_supersedes_quiets_and_resolves(pg_factory) -> None:
    now = datetime.now(UTC)
    async with pg_factory() as session:
        service = NotificationService(session, repeat_minutes=60)
        first = await service.emit(_event(), now=now)
        again = await service.emit(_event(), now=now + timedelta(seconds=1))
        assert again["id"] == first["id"] and first["push_status"] == "pending" and first["status"] == "open"
        second = await service.emit(_event(body="new code"), now=now + timedelta(minutes=1))
        rows = await _rows(session, "t")
        assert [row.status for row in rows] == ["superseded", "open"]
        assert second["push_status"] == "pending"
        assert rows[1].push_quiet is False  # nothing was actually sent yet
        rows[0].push_status, rows[0].push_attempted_at = "sent", now
        await session.flush()
        third = await service.emit(_event(body="third code"), now=now + timedelta(minutes=2))
        third_row = await session.get(NotificationRow, UUID(third["id"]))
        assert third_row.push_quiet is True
        resolved = await service.emit(_event(severity="resolved", kind="done", title="Done"), now=now + timedelta(minutes=3))
        assert resolved["status"] == "resolved" and resolved["push_status"] == "pending"
        rows = await _rows(session, "t")
        assert [row.status for row in rows] == ["superseded", "superseded", "resolved", "resolved"]
        assert await service.emit(_event(severity="resolved", kind="done", title="Done")) is None
        info = await service.emit(_event(severity="info", thread_key=None))
        assert info["push_status"] == "none"
        await session.commit()


@pytest.mark.asyncio
async def test_inbox_listing_read_state_and_model_redaction(pg_factory) -> None:
    async with pg_factory() as session:
        service = NotificationService(session)
        secret = await service.emit(NotificationEvent(source="runtime.rdc", kind="auth_required", severity="action_required",
            title="Needs code", body="Enter AB12-CD34", detail={"code": "AB12-CD34", "url": "https://x"},
            sensitive_fields=("code", "body"), thread_key="rdc.auth:1"))
        await service.emit(_event(thread_key="other"))
        await service.emit(_event(body="v2", thread_key="other"))
        owner = await service.list()
        assert [item["thread_key"] for item in owner] == ["other", "rdc.auth:1"]
        assert len(await service.list(include_superseded=True)) == 3
        model = await service.list(audience="model")
        redacted = next(item for item in model if item["id"] == secret["id"])
        assert redacted["detail"]["code"] == "[redacted]" and redacted["body"] == "[redacted]"
        assert redacted["detail"]["url"] == "https://x"
        assert await service.unread_count() == 2
        await service.mark_read(UUID(secret["id"]))
        assert await service.unread_count() == 1
        assert await service.mark_all_read() == 2  # the superseded row is unread too but not counted
        assert await service.unread_count() == 0
        resolved = await service.resolve(UUID(secret["id"]))
        assert resolved["status"] == "resolved"
        assert await service.model_emit_allowed(1) is True
        await service.emit(_event(source="model", thread_key=None))
        assert await service.model_emit_allowed(1) is False
        with pytest.raises(LookupError):
            await service.mark_read(uuid4())
        await session.commit()


@pytest.mark.asyncio
async def test_attention_rows_are_mirrored_and_resolved_through_the_flush_hook(pg_factory) -> None:
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner")
        session.add(transcript)
        await session.flush()
        store = AuthorityStore(session)
        run_id = await store.create_run(transcript_id=transcript.id, intent="test", kind=RunKind.FOREGROUND)
        action_id = await store.prepare_proposal(run_id=run_id, operation="gmail.message.send",
            arguments={"to": "a@b", "subject": "s", "body": "b"}, title="Send test email", capability_id="google.workspace")
        await session.commit()
        rows = await _rows(session, f"action:{action_id}")
        assert len(rows) == 1 and rows[0].severity == "action_required" and rows[0].source == "authority"
        assert rows[0].title == "Send test email" and rows[0].run_id == run_id
        assert "arguments" not in rows[0].detail and rows[0].detail["operation"] == "gmail.message.send"
        action = await store.action_for_decision(action_id)
        await store.cancel(action)
        await session.commit()
        rows = await _rows(session, f"action:{action_id}")
        assert rows[0].status == "resolved" and rows[0].resolved_at is not None

        interrupted = OwnerAttentionRow(run_id=run_id, state="interrupted", title="Atlas work was interrupted",
            detail={"message": "stopped", "resume": "later"})
        session.add(interrupted)
        await session.commit()
        rows = await _rows(session, f"run:{run_id}:interrupted")
        assert len(rows) == 1 and rows[0].severity == "warning" and rows[0].body == "stopped"
        assert rows[0].detail["attention_id"] == str(interrupted.id)
        interrupted.resolved = True
        interrupted.resolved_at = datetime.now(UTC)
        await session.commit()
        rows = await _rows(session, f"run:{run_id}:interrupted")
        assert rows[0].status == "resolved"

        staged = OwnerAttentionRow(run_id=run_id, state="staged_change", title="Project change ready to review",
            detail={"message": "bundle", "download_url": "/api/project-changes/x", "path": "a.py"})
        session.add(staged)
        await session.commit()
        rows = await _rows(session, f"attention:{staged.id}")
        assert len(rows) == 1 and rows[0].severity == "info" and rows[0].push_status == "none"


@pytest.mark.asyncio
async def test_outbox_delivers_to_every_device_and_disables_gone_subscriptions(pg_factory) -> None:
    async with pg_factory() as session:
        session.add_all([
            PushSubscriptionRow(endpoint="https://push.example/ok", p256dh="p", auth="a", user_agent="phone"),
            PushSubscriptionRow(endpoint="https://push.example/gone", p256dh="p", auth="a", user_agent="old"),
            PushSubscriptionRow(endpoint="https://push.example/off", p256dh="p", auth="a", disabled_at=datetime.now(UTC)),
        ])
        service = NotificationService(session)
        pending = await service.emit(_event(thread_key="d1"))
        quiet_info = await service.emit(_event(severity="info", thread_key=None))
        await session.commit()

    class FakeClient:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict]] = []

        def send(self, info, payload, *, ttl=900):
            self.sent.append((info["endpoint"], payload))
            return 410 if info["endpoint"].endswith("gone") else 201

    client = FakeClient()
    assert await deliver_pending(pg_factory, client) == 1
    assert await deliver_pending(pg_factory, client) == 0
    assert sorted(endpoint for endpoint, _ in client.sent) == ["https://push.example/gone", "https://push.example/ok"]
    assert client.sent[0][1]["tag"] == "d1" and client.sent[0][1]["title"] == "Title"
    async with pg_factory() as session:
        row = await session.get(NotificationRow, UUID(pending["id"]))
        assert row.push_status == "sent" and len(row.push_result) == 2
        assert sorted(item["status"] for item in row.push_result.values()) == [201, 410]
        assert (await session.get(NotificationRow, UUID(quiet_info["id"]))).push_status == "none"
        subs = {row.endpoint: row for row in (await session.execute(select(PushSubscriptionRow))).scalars().all()}
        assert subs["https://push.example/gone"].disabled_at is not None
        assert subs["https://push.example/ok"].last_success_at is not None and subs["https://push.example/ok"].failure_count == 0


@pytest.mark.asyncio
async def test_outbox_without_devices_or_client_marks_rows_skipped(pg_factory) -> None:
    async with pg_factory() as session:
        pending = await NotificationService(session).emit(_event(thread_key="d2"))
        await session.commit()
    assert await deliver_pending(pg_factory, None) == 1
    async with pg_factory() as session:
        assert (await session.get(NotificationRow, UUID(pending["id"]))).push_status == "skipped"


@pytest.mark.asyncio
async def test_rdc_monitor_pass_persists_cursor_state_and_events_atomically(pg_factory) -> None:
    settings = Settings(owner_timezone="Africa/Johannesburg", rdc_monitor_uid=1000)
    now = datetime.now(UTC)
    entries = load_replay_entries(FIXTURE, now=now)
    code_flow = entries[:12]
    online = entries[12:]

    async def first_reader(cursor):
        assert cursor is None
        return code_flow, "cursor-1"

    async def second_reader(cursor):
        assert cursor == "cursor-1"
        return online, "cursor-2"

    async def empty_reader(cursor):
        assert cursor == "cursor-2"
        return [], None

    config = RdcConfig(timezone="Africa/Johannesburg")
    result = await rdc_monitor_once(settings, pg_factory, journal_reader=first_reader, liveness=lambda: True, config=config, now=now)
    assert result["emitted"] == 1 and result["phase"] == "auth_required"
    async with pg_factory() as session:
        state = await session.get(HostMonitorStateRow, "rdc")
        assert state.cursor == "cursor-1" and state.state["code"] == "AB12-CD34"
        rows = await _rows(session, "rdc.auth:11111111-2222-4333-8444-555555555555")
        assert len(rows) == 1 and rows[0].severity == "action_required" and rows[0].push_status == "pending"
    result = await rdc_monitor_once(settings, pg_factory, journal_reader=second_reader, liveness=lambda: True, config=config, now=now)
    assert result["emitted"] == 1 and result["phase"] == "online"
    async with pg_factory() as session:
        rows = await _rows(session, "rdc.auth:11111111-2222-4333-8444-555555555555")
        assert [row.status for row in rows] == ["resolved", "resolved"]
        assert rows[1].severity == "resolved" and rows[1].push_status == "pending"
        assert (await session.get(HostMonitorStateRow, "rdc")).cursor == "cursor-2"
    result = await rdc_monitor_once(settings, pg_factory, journal_reader=empty_reader, liveness=lambda: True, config=config, now=now)
    assert result["emitted"] == 0
    async with pg_factory() as session:
        assert (await session.get(HostMonitorStateRow, "rdc")).cursor == "cursor-2"
        assert isinstance((await session.execute(select(RunRow))).scalars().all(), list)
