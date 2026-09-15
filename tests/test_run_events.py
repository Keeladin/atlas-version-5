from uuid import uuid4

import pytest
from atlas.artifacts.store import ArtifactStore
from atlas.persistence.models import RunEventRow, RunRow
from atlas.runtime.recovery import interrupt_run
from atlas.runtime.run_events import append_run_event, run_events_after
from sqlalchemy import select


@pytest.mark.asyncio
async def test_run_events_are_durable_ordered_and_resumable(pg_factory) -> None:
    run_id = uuid4()
    async with pg_factory() as session:
        session.add(RunRow(id=run_id, kind="foreground", status="running", inference_active=True, inference_status="running"))
        await session.flush()
        first = await append_run_event(session, run_id, "accepted", {"owner_turn_id": "turn-1"})
        second = await append_run_event(session, run_id, "delta", {"text": "hello"})
        third = await append_run_event(session, run_id, "completed", {})
        await session.commit()
        first_id, second_id, third_id = first.id, second.id, third.id

    assert first_id < second_id < third_id
    async with pg_factory() as session:
        replay = await run_events_after(session, run_id, first_id)
    assert [(row.id, row.event_type) for row in replay] == [(second_id, "delta"), (third_id, "completed")]


@pytest.mark.asyncio
async def test_runtime_interruption_persists_owner_event_independent_of_transport(pg_factory, tmp_path) -> None:
    run_id = uuid4()
    async with pg_factory() as session:
        session.add(RunRow(id=run_id, kind="foreground", status="running", inference_active=True, inference_status="running"))
        await session.commit()

    await interrupt_run(pg_factory, ArtifactStore(tmp_path), run_id, reason="observer disconnected")

    async with pg_factory() as session:
        run = await session.get(RunRow, run_id)
        events = (await session.execute(select(RunEventRow).where(RunEventRow.run_id == run_id))).scalars().all()
    assert run is not None and run.inference_status == "interrupted" and run.inference_active is False
    assert [(row.event_type, row.payload) for row in events] == [
        ("interrupted", {"message": "observer disconnected"})
    ]

@pytest.mark.asyncio
async def test_conversation_snapshot_exposes_active_foreground_run(pg_factory, monkeypatch) -> None:
    import atlas.api.app as app_module
    from atlas.transcript.repository import TranscriptRepository

    monkeypatch.setattr(app_module, "get_session_factory", lambda: pg_factory)
    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        transcript = await repo.create_owner_chat(title="Reconnect test")
        run_id = await __import__('atlas.actions.authority', fromlist=['AuthorityStore']).AuthorityStore(session).create_run(
            transcript_id=transcript.id, intent="Long turn"
        )
        await session.commit()
        payload = await app_module.conversation(
            session, chat_id=transcript.id, before_sequence=None, limit=200
        )

    assert payload["active_run"]["id"] == str(run_id)
    assert payload["active_run"]["inference_active"] is True
    assert payload["active_run"]["events_url"] == f"/api/runs/{run_id}/events"


@pytest.mark.asyncio
async def test_sse_replays_after_server_owned_cursor_and_closes_on_terminal(pg_factory, monkeypatch) -> None:
    import atlas.api.app as app_module
    from starlette.requests import Request

    monkeypatch.setattr(app_module, "get_session_factory", lambda: pg_factory)
    run_id = uuid4()
    async with pg_factory() as session:
        run = RunRow(id=run_id, kind="foreground", status="succeeded", inference_active=False, inference_status="succeeded")
        session.add(run)
        await session.flush()
        accepted = await append_run_event(session, run_id, "accepted", {})
        await append_run_event(session, run_id, "delta", {"text": "after refresh"})
        await append_run_event(session, run_id, "completed", {})
        await session.commit()
        cursor = accepted.id

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({
        "type": "http", "method": "GET", "path": f"/api/runs/{run_id}/events",
        "headers": [], "query_string": b"", "client": ("test", 1), "server": ("test", 80), "scheme": "http",
    }, receive=receive)
    response = await app_module.run_event_stream(run_id, request, after=cursor)
    body = b"".join([chunk.encode() if isinstance(chunk, str) else chunk async for chunk in response.body_iterator]).decode()

    assert "event: accepted" not in body
    assert "event: delta" in body and '"text":"after refresh"' in body
    assert "event: completed" in body
