"""Publisher replay: an identical publication retried with the same operation id
must be answered from the shared-write ledger without a second semantic mutation.

Every scenario drives a real explicit remember/correct through the verification
pipeline, captures the exact publish call, and replays it.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from atlas.memory.durable import DurableMemoryRepository
from atlas.memory.service import MemoryService
from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryCommandRow,
    MemoryObligationRow,
    MemoryProvenanceRow,
    MemoryReconciliationAttemptRow,
    SharedResourceVersionRow,
    SharedWriteOperationRow,
)
from sqlalchemy import func, select
from test_memory_explicit_commands import (
    _append_owner_turn,
    _OwnerClaimModel,
    _publish_ok,
    _published_memory_id,
    _seed_owner_turn,
    _service,
)


async def _graph_state(pg_factory) -> dict[str, object]:
    async with pg_factory() as session:
        memories = [
            (row.id, row.status, row.content, row.supersedes_id, row.superseded_by_id,
             row.valid_from, row.valid_to, row.grounding_status, row.updated_at)
            for row in (await session.execute(
                select(DurableMemoryRow).order_by(DurableMemoryRow.created_at, DurableMemoryRow.id)
            )).scalars()
        ]
        provenance = (await session.execute(
            select(func.count()).select_from(MemoryProvenanceRow)
        )).scalar_one()
        candidates = [
            (row.id, row.status, int(row.state_version or 0), row.decision_json)
            for row in (await session.execute(
                select(MemoryCandidateRow).order_by(MemoryCandidateRow.created_at, MemoryCandidateRow.id)
            )).scalars()
        ]
        attempts = (await session.execute(
            select(func.count()).select_from(MemoryReconciliationAttemptRow)
        )).scalar_one()
        obligations = [
            (row.id, row.status, row.resolution_code, row.resolved_at, row.resolution_json)
            for row in (await session.execute(
                select(MemoryObligationRow).order_by(MemoryObligationRow.created_at, MemoryObligationRow.id)
            )).scalars()
        ]
        commands = [
            (row.id, row.status, row.replacement_memory_id, row.applied_at)
            for row in (await session.execute(select(MemoryCommandRow))).scalars()
        ]
        version = await session.get(SharedResourceVersionRow, ("memory_state", "owner"))
        operations = (await session.execute(
            select(func.count()).select_from(SharedWriteOperationRow)
        )).scalar_one()
        return {
            "memories": memories,
            "provenance": provenance,
            "candidates": candidates,
            "attempts": attempts,
            "obligations": obligations,
            "commands": commands,
            "version": int(version.version) if version is not None else None,
            "operations": operations,
        }


async def _capture_publish(service, monkeypatch) -> dict:
    """Pin the publish operation id and record the exact call and receipt."""
    captured: dict = {}
    original = service.publisher.publish

    async def capturing(claim, snapshot, decision, *, operation_id=None):
        operation_id = operation_id or uuid4()
        receipt = await original(claim, snapshot, decision, operation_id=operation_id)
        captured.update(
            claim=claim, snapshot=snapshot, decision=decision,
            operation_id=operation_id, receipt=receipt, original=original,
        )
        return receipt

    monkeypatch.setattr(service.publisher, "publish", capturing)
    return captured


async def _seed_scenario(pg_factory, scenario: str) -> tuple[_OwnerClaimModel, str]:
    """Return the verifier for the final publication and the expected result code."""
    service = MemoryService(pg_factory)
    transcript_id, _ = await _seed_owner_turn(pg_factory, "Remember that I live in London.")
    if scenario == "create":
        await service.remember({"content": "I live in London."})
        return _OwnerClaimModel(pg_factory), "created"

    if scenario == "legacy_grounding":
        async with pg_factory() as session:
            legacy, _ = await DurableMemoryRepository(session).create_active(
                "I live in London.",
                source_transcript_id=None, source_turn_id=None,
                record_kind="derived", origin="import",
                grounding_status="legacy_unverified",
                memory_kind="fact", scope="cross_chat", scope_key="owner",
                durability="long_term",
            )
            await session.commit()
            legacy_id = legacy.id
        await service.remember({"content": "I live in London.", "kind": "fact"})
        model = _OwnerClaimModel(pg_factory, reconcile=lambda candidate: {
            "relation": "grounds_legacy",
            "target_memory_id": str(legacy_id),
            "replacement_content": candidate.content,
        })
        return model, "legacy_grounded"

    queued = await service.remember({"content": "I live in London."})
    await _publish_ok(pg_factory)
    first_id = await _published_memory_id(pg_factory, queued)

    if scenario == "duplicate_lineage":
        await _append_owner_turn(pg_factory, transcript_id, "As I said, I live in London.")
        await service.remember({"content": "I live in London."})
        return _OwnerClaimModel(pg_factory), "lineage_added"

    if scenario == "supersede":
        await _append_owner_turn(pg_factory, transcript_id, "I moved and now live in Lisbon.")
        await service.correct({
            "memory_id": str(first_id), "content": "I live in Lisbon.",
            "change_type": "change_over_time",
        })
        return _OwnerClaimModel(pg_factory), "supersede"

    if scenario == "historical_predecessor":
        await _append_owner_turn(pg_factory, transcript_id, "Before London I lived in Paris.")
        await service.remember({"content": "I lived in Paris before London."})
        model = _OwnerClaimModel(
            pg_factory,
            reconcile=lambda candidate: {
                "relation": "historical_predecessor",
                "target_memory_id": str(first_id),
                "replacement_content": candidate.content,
            },
            event_time=lambda observed: observed - timedelta(days=30),
        )
        return model, "historical_predecessor"

    raise AssertionError(scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["create", "duplicate_lineage", "supersede", "historical_predecessor", "legacy_grounding"],
)
async def test_publication_replay_is_answered_from_ledger_without_second_mutation(
    pg_factory, monkeypatch, scenario
):
    model, expected = await _seed_scenario(pg_factory, scenario)
    service = _service(pg_factory, model)
    captured = await _capture_publish(service, monkeypatch)

    result = await service.run_once()
    assert result.reconciled == 1, result
    first = captured["receipt"]
    assert first.replayed is False
    assert first.result["result"] == expected
    assert first.outcome == "applied"
    before = await _graph_state(pg_factory)
    assert before["version"] == first.committed_version

    replay = await captured["original"](
        captured["claim"], captured["snapshot"], captured["decision"],
        operation_id=captured["operation_id"],
    )
    assert replay.replayed is True
    assert replay.operation_id == first.operation_id
    assert replay.result == first.result
    assert replay.committed_version == first.committed_version
    assert replay.outcome == first.outcome

    after = await _graph_state(pg_factory)
    assert after == before

    # And a fresh operation id for the same stale claim cannot mutate either:
    # the lease is spent, so the publisher reports a conflict, not a second write.
    stale = await captured["original"](
        captured["claim"], captured["snapshot"], captured["decision"], operation_id=uuid4(),
    )
    assert stale.replayed is False
    assert stale.outcome in {"version_conflict", "no_change"}
    if stale.outcome == "no_change":
        assert stale.result["result"] == "lease_conflict"
    after_stale = await _graph_state(pg_factory)
    # The fresh operation id claims its own ledger row (recording the conflict);
    # nothing else in the graph moves.
    assert after_stale["operations"] == before["operations"] + 1
    assert {key: value for key, value in after_stale.items() if key != "operations"} == {
        key: value for key, value in before.items() if key != "operations"
    }


@pytest.mark.asyncio
async def test_replay_receipt_memory_id_matches_published_memory(pg_factory, monkeypatch):
    model, _ = await _seed_scenario(pg_factory, "create")
    service = _service(pg_factory, model)
    captured = await _capture_publish(service, monkeypatch)
    assert (await service.run_once()).reconciled == 1
    replay = await captured["original"](
        captured["claim"], captured["snapshot"], captured["decision"],
        operation_id=captured["operation_id"],
    )
    async with pg_factory() as session:
        memory = (await session.execute(select(DurableMemoryRow))).scalar_one()
        obligation = (await session.execute(
            select(MemoryObligationRow).where(MemoryObligationRow.kind == "explicit_remember")
        )).scalar_one()
    assert UUID(str(replay.result["memory_id"])) == memory.id
    assert obligation.resolution_json["memory_id"] == str(memory.id)
