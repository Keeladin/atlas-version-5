from __future__ import annotations

import json
import stat

import pytest
from atlas.memory.observer import write_observer_snapshot
from test_memory_state_machine import _seed


@pytest.mark.asyncio
async def test_observer_snapshot_reuses_control_projection_and_is_bounded(
    pg_factory, tmp_path
) -> None:
    await _seed(
        pg_factory,
        content="Jaco prefers architecture before implementation details.",
        kind="preference",
        durability="long_term",
    )
    path = tmp_path / "observer" / "memory.json"
    written = await write_observer_snapshot(pg_factory, path=path, limit=1)
    assert written == path
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    assert payload["limit"] == 1
    assert payload["generated_at"]
    assert len(payload["overview"]["recent_candidates"]) == 1
    assert len(payload["candidate_details"]) == 1

    listed = payload["overview"]["recent_candidates"][0]
    detail = payload["candidate_details"][0]
    assert detail["candidate"]["id"] == listed["id"]
    assert detail["candidate"]["content"] == listed["content"]
    assert "verification" in detail
    assert "attempts" in detail
    assert "linked_memories" in detail

    text = path.read_text().lower()
    assert "openai_api_key" not in text
    assert "database_url" not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
