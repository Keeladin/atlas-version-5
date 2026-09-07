from atlas.api.app import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_bootstrap_endpoint() -> None:
    response = client.get("/api/bootstrap")
    assert response.status_code == 200
    body = response.json()
    assert body["identity"] == "Atlas"
    assert body["environment_registry_available"] is True


def test_registry_endpoint_exposes_enabled_projection_only() -> None:
    response = client.get("/api/registry")
    assert response.status_code == 200
    capabilities = response.json()["capabilities"]
    assert [item["id"] for item in capabilities] == ["atlas.artifacts", "atlas.local_storage", "atlas.project_folders", "atlas.schedules"]


def test_control_route_serves_spa_entrypoint(tmp_path, monkeypatch) -> None:
    import atlas.api.app as app_module

    (tmp_path / "index.html").write_text('<div id="root"></div>')
    monkeypatch.setattr(app_module.settings, "frontend_dist", tmp_path)

    response = client.get("/control")
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text

def test_context_pressure_states() -> None:
    from atlas.api.app import _context_pressure_state

    assert _context_pressure_state(699_999, 1_000_000) == "green"
    assert _context_pressure_state(700_000, 1_000_000) == "amber"
    assert _context_pressure_state(900_000, 1_000_000) == "red"


def test_control_restart_endpoint_requests_supervised_restart(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("atlas.api.app._restart_api_process", lambda: calls.append("restart"))

    response = client.post("/api/control/restart")

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
    assert records[0]["message"]["role"] == "developer"


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
    monkeypatch.setattr("atlas.api.app.settings.working_context_tokens", 2600)
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
