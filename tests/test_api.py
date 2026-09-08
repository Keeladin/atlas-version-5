import httpx
import pytest
from atlas.api.app import app


async def app_request(method: str, path: str):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path)


@pytest.mark.asyncio
async def test_bootstrap_endpoint() -> None:
    response = await app_request("GET", "/api/bootstrap")
    assert response.status_code == 200
    body = response.json()
    assert body["identity"] == "Atlas"
    assert body["environment_registry_available"] is True


@pytest.mark.asyncio
async def test_registry_endpoint_exposes_enabled_projection_only(monkeypatch) -> None:
    async def owner_enabled():
        return {"atlas.artifacts", "atlas.evidence", "atlas.local_storage", "atlas.project_folders", "atlas.schedules"}
    monkeypatch.setattr("atlas.api.app.capability_runtime.enabled_capabilities", owner_enabled)
    response = await app_request("GET", "/api/registry")
    assert response.status_code == 200
    capabilities = response.json()["capabilities"]
    assert [item["id"] for item in capabilities] == ["atlas.artifacts", "atlas.evidence", "atlas.local_storage", "atlas.project_folders", "atlas.schedules"]


@pytest.mark.asyncio
async def test_control_route_serves_spa_entrypoint(tmp_path, monkeypatch) -> None:
    import atlas.api.app as app_module

    (tmp_path / "index.html").write_text('<div id="root"></div>')
    monkeypatch.setattr(app_module.settings, "frontend_dist", tmp_path)

    response = await app_request("GET", "/control")
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text

def test_context_pressure_states() -> None:
    from atlas.api.app import _context_pressure_state

    assert _context_pressure_state(699_999, 1_000_000) == "green"
    assert _context_pressure_state(700_000, 1_000_000) == "amber"
    assert _context_pressure_state(900_000, 1_000_000) == "red"


@pytest.mark.asyncio
async def test_control_restart_endpoint_requests_supervised_restart(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("atlas.api.app._restart_api_process", lambda: calls.append("restart"))

    response = await app_request("POST", "/api/control/restart")

    assert response.status_code == 202
    assert response.json() == {"status": "restarting"}
    assert calls == ["restart"]


def test_recent_exchange_turns_starts_at_requested_owner_message() -> None:
    from uuid import uuid4

    from atlas.api.app import _recent_exchange_turns, _turn_statistics
    from atlas.transcript.models import Actor, TextBlock, ToolObservationBlock, Turn

    transcript_id = uuid4()
    turns = []
    for index in range(4):
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.OWNER, blocks=[TextBlock(text=f"owner {index}")]))
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.TOOL, blocks=[ToolObservationBlock(operation="demo", detail={})]))
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.ATLAS, blocks=[TextBlock(text=f"atlas {index}")]))

    selected = _recent_exchange_turns(turns, 2)
    stats = _turn_statistics(selected)

    assert selected[0].actor == Actor.OWNER
    assert selected[0].blocks[0].text == "owner 2"
    assert stats["owner_messages"] == 2
    assert stats["atlas_messages"] == 2
    assert stats["tool_observations"] == 2
    assert stats["transcript_turns"] == 6


def test_tool_evidence_records_track_exchange_age_and_payload() -> None:
    from uuid import uuid4

    from atlas.api.app import _tool_band, _tool_evidence_records
    from atlas.transcript.models import Actor, TextBlock, ToolObservationBlock, Turn

    transcript_id = uuid4()
    turns = []
    for index in range(16):
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.OWNER, blocks=[TextBlock(text=f"owner {index}")]))
        if index in {0, 5, 15}:
            turns.append(Turn(
                transcript_id=transcript_id,
                actor=Actor.TOOL,
                blocks=[ToolObservationBlock(operation=f"demo.{index}", phase="succeeded", detail={"value": index})],
            ))
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.ATLAS, blocks=[TextBlock(text=f"atlas {index}")]))

    records = _tool_evidence_records(turns)

    assert [record["exchange_age"] for record in records] == [16, 11, 1]
    assert [_tool_band(int(record["exchange_age"])) for record in records] == [
        "exchanges_16_20", "exchanges_11_15", "last_10"
    ]
    assert records[0]["operation"] == "demo.0"
    assert records[0]["payload_characters"] > 0
    assert records[0]["message"]["role"] == "user"


def test_working_context_compacts_old_successful_tools_before_trimming(monkeypatch) -> None:
    import asyncio
    from uuid import uuid4

    from atlas.api.app import _assemble_working_context
    from atlas.transcript.models import (
        Actor,
        TextBlock,
        ToolObservationBlock,
        Transcript,
        Turn,
    )

    class FakeProvider:
        async def count_input_tokens(self, *, instructions, messages):
            return 100 + sum(len(str(message.get("content", ""))) for message in messages)

    transcript_id = uuid4()
    turns = []
    for index in range(3):
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.OWNER, blocks=[TextBlock(text=f"owner {index}")]))
        turns.append(Turn(
            transcript_id=transcript_id, actor=Actor.TOOL,
            blocks=[ToolObservationBlock(operation="storage.projects.acquire", phase="succeeded", detail={"payload": "x" * 1800})],
        ))
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.ATLAS, blocks=[TextBlock(text=f"atlas {index}")]))

    monkeypatch.setattr("atlas.api.app.settings.working_context_exchanges", 3)
    monkeypatch.setattr("atlas.api.app.settings.working_context_tokens", 4534)  # 75% initial seat is 3400 tokens
    monkeypatch.setattr("atlas.api.app.settings.working_context_raw_tool_exchanges", 1)

    messages, policy = asyncio.run(_assemble_working_context(FakeProvider(), Transcript(id=transcript_id), turns))

    assert policy["selected_exchanges"] == 3
    assert policy["compacted_tool_turns"] == 2
    assert policy["stage"] == "older_tools_compacted"
    assert policy["budget_exceeded"] is False
    assert sum("(compacted)" in message.get("content", "") for message in messages) == 2


def test_working_context_trims_oldest_exchange_if_compaction_is_not_enough(monkeypatch) -> None:
    import asyncio
    from uuid import uuid4

    from atlas.api.app import _assemble_working_context
    from atlas.transcript.models import Actor, TextBlock, Transcript, Turn

    class FakeProvider:
        async def count_input_tokens(self, *, instructions, messages):
            return 100 + sum(len(str(message.get("content", ""))) for message in messages)

    transcript_id = uuid4()
    turns = []
    for index in range(3):
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.OWNER, blocks=[TextBlock(text="o" * 700)]))
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.ATLAS, blocks=[TextBlock(text="a" * 700)]))

    monkeypatch.setattr("atlas.api.app.settings.working_context_exchanges", 3)
    monkeypatch.setattr("atlas.api.app.settings.working_context_tokens", 2000)

    _, policy = asyncio.run(_assemble_working_context(FakeProvider(), Transcript(id=transcript_id), turns))

    assert policy["stage"] == "history_trimmed"
    assert policy["selected_exchanges"] == 1
    assert policy["budget_exceeded"] is False


def test_working_context_never_evicts_active_task_checkpoint(monkeypatch) -> None:
    import asyncio
    from uuid import uuid4

    from atlas.api.app import _assemble_working_context
    from atlas.runtime.task_state import (
        TaskStateDelta,
        merge_semantic_delta,
        new_task_state,
    )
    from atlas.transcript.models import Actor, TextBlock, Transcript, Turn

    class FakeProvider:
        async def count_input_tokens(self, *, instructions, messages):
            return 100 + sum(len(str(message.get("content", ""))) for message in messages)

    transcript_id = uuid4()
    turns = []
    for index in range(4):
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.OWNER, blocks=[TextBlock(text="o" * 900)]))
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.ATLAS, blocks=[TextBlock(text="a" * 900)]))
    task_state = merge_semantic_delta(
        new_task_state("Keep continuity"),
        TaskStateDelta(objective="Finish the active multi-step task", next_step="Run verification"),
    )

    monkeypatch.setattr("atlas.api.app.settings.working_context_exchanges", 4)
    monkeypatch.setattr("atlas.api.app.settings.working_context_tokens", 2800)

    messages, policy = asyncio.run(
        _assemble_working_context(FakeProvider(), Transcript(id=transcript_id, active_task_state=task_state), turns)
    )

    assert policy["stage"] == "history_trimmed"
    assert policy["selected_exchanges"] < 4
    task_messages = [message for message in messages if "Protected active-task checkpoint" in str(message.get("content", ""))]
    assert len(task_messages) == 1
    assert "Finish the active multi-step task" in task_messages[0]["content"]
    assert "Run verification" in task_messages[0]["content"]

def test_cross_chat_continuity_is_dropped_before_current_chat_history(monkeypatch) -> None:
    import asyncio
    from uuid import uuid4

    from atlas.api.app import _assemble_working_context
    from atlas.transcript.models import Actor, TextBlock, Transcript, Turn

    class FakeProvider:
        async def count_input_tokens(self, *, instructions, messages):
            return 100 + sum(len(str(message.get("content", ""))) for message in messages)

    transcript_id = uuid4()
    turns = []
    for index in range(2):
        turns.append(Turn(
            transcript_id=transcript_id, actor=Actor.OWNER,
            blocks=[TextBlock(text=f"owner {index} " + "o" * 440)],
        ))
        turns.append(Turn(
            transcript_id=transcript_id, actor=Actor.ATLAS,
            blocks=[TextBlock(text=f"atlas {index} " + "a" * 440)],
        ))

    monkeypatch.setattr("atlas.api.app.settings.working_context_exchanges", 2)
    monkeypatch.setattr("atlas.api.app.settings.working_context_tokens", 3000)

    messages, policy = asyncio.run(_assemble_working_context(
        FakeProvider(), Transcript(id=transcript_id), turns,
        continuity_context="prior chat orientation " + "c" * 1500, continuity_count=3,
    ))

    assert policy["stage"] == "continuity_omitted"
    assert policy["continuity_included"] is False
    assert policy["continuity_chats"] == 0
    assert policy["selected_exchanges"] == 2
    assert policy["budget_exceeded"] is False
    assert all("cross-chat continuity orientation" not in str(message.get("content", "")) for message in messages)
