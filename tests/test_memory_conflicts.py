"""Memory conflicts: owner-facing projection, resolution, expiry, attention, and the
conversational round trip through the capability runtime."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from atlas.actions.authority import AuthorityStore
from atlas.artifacts.store import ArtifactStore
from atlas.capabilities import CapabilityRuntime
from atlas.memory.lifecycle import MemoryLifecycleCommands
from atlas.memory.reconciliation import (
    MemoryCandidateLeaseRepository,
    MemoryReconciliationService,
)
from atlas.memory.service import MemoryService
from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryCommandRow,
    MemoryConflictRow,
    MemoryObligationRow,
    MemoryProvenanceRow,
    TurnRow,
)
from atlas.registry.service import build_phase0_registry
from atlas.runtime.conversation import (
    render_memory_attention,
    turns_to_provider_messages,
)
from atlas.runtime.execution import RunExecutor
from sqlalchemy import func, select
from test_memory_explicit_commands import (
    _append_owner_turn,
    _OwnerClaimModel,
    _publish_ok,
    _rows,
    _seed_owner_turn,
    _service,
)
from test_memory_state_machine import _create_verified_derived, _PipelineModel, _seed

CURRENT = "Jaco prefers detailed shift reports."
COMPETING = "Jaco prefers compact shift reports."


async def _raise_conflict(pg_factory, *, current: str = CURRENT, competing: str = COMPETING):
    target_id = await _create_verified_derived(pg_factory, content=current)
    candidate_id, transcript_id, turn_id, _ = await _seed(pg_factory, content=competing)
    model = _PipelineModel(content=competing, relation="conflicts_with", target_memory_id=target_id)
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.blocked == 1, result
    async with pg_factory() as session:
        conflict = (await session.execute(select(MemoryConflictRow))).scalar_one()
        obligation = (await session.execute(
            select(MemoryObligationRow).where(MemoryObligationRow.kind == "memory_conflict")
        )).scalar_one()
        assert conflict.status == "open"
        assert obligation.status == "pending"
    return SimpleNamespace(
        target_id=target_id, candidate_id=candidate_id, transcript_id=transcript_id,
        turn_id=turn_id, conflict_id=conflict.id, obligation_id=obligation.id,
    )


async def _listed_conflict(lifecycle: MemoryLifecycleCommands, obligation_id: UUID) -> dict:
    listing = await lifecycle.obligations({"kind": "memory_conflict"})
    return next(item for item in listing["obligations"] if item["obligation_id"] == str(obligation_id))


async def _conflict_state(pg_factory, seeded) -> tuple[MemoryConflictRow, MemoryObligationRow, MemoryCandidateRow, DurableMemoryRow]:
    async with pg_factory() as session:
        return (
            await session.get(MemoryConflictRow, seeded.conflict_id),
            await session.get(MemoryObligationRow, seeded.obligation_id),
            await session.get(MemoryCandidateRow, seeded.candidate_id),
            await session.get(DurableMemoryRow, seeded.target_id),
        )


async def _search_conflicts(pg_factory) -> list[dict]:
    found = await MemoryService(pg_factory).search({"query": "shift reports", "limit": 5})
    return found["memory_conflicts"]


@pytest.mark.asyncio
async def test_conflict_obligation_lists_competing_claim_target_and_review_version(pg_factory):
    seeded = await _raise_conflict(pg_factory)
    item = await _listed_conflict(MemoryLifecycleCommands(pg_factory), seeded.obligation_id)
    assert item["kind"] == "memory_conflict"
    assert item["subject_type"] == "memory_conflict"
    assert item["conflict_id"] == str(seeded.conflict_id)
    assert item["conflict_status"] == "open"
    assert item["target_memory_id"] == str(seeded.target_id)
    assert item["target_content"] == CURRENT
    assert item["target_authority"] == "derived"
    assert item["competing_claim"] == COMPETING
    assert item["candidate_id"] == str(seeded.candidate_id)
    assert item["resolution_options"] == ["accept_competing", "keep_current", "restate"]
    assert isinstance(item["review_version"], str) and len(item["review_version"]) == 64
    assert item["proposed_assertion_text"] is None
    surfaced = await _search_conflicts(pg_factory)
    assert len(surfaced) == 1
    assert surfaced[0]["target_content"] == CURRENT
    assert surfaced[0]["resolution_options"] == ["accept_competing", "keep_current", "restate"]


@pytest.mark.asyncio
async def test_keep_current_rejects_competing_and_stops_surfacing_conflict(pg_factory):
    seeded = await _raise_conflict(pg_factory)
    lifecycle = MemoryLifecycleCommands(pg_factory)
    item = await _listed_conflict(lifecycle, seeded.obligation_id)
    resolved = await lifecycle.resolve_obligation({
        "obligation_id": str(seeded.obligation_id),
        "decision": "keep_current",
        "review_version": item["review_version"],
    })
    assert resolved["result"] == "keep_current"
    assert resolved["target_memory_id"] == str(seeded.target_id)
    assert "_context_suppression" not in resolved

    conflict, obligation, candidate, target = await _conflict_state(pg_factory, seeded)
    assert conflict.status == "resolved_keep_current"
    assert conflict.resolved_at is not None
    assert obligation.status == "resolved"
    assert obligation.resolution_code == "keep_current"
    assert obligation.resolution_json["candidate_id"] == str(seeded.candidate_id)
    assert candidate.status == "rejected"
    assert candidate.decision_json["code"] == "owner_kept_current"
    assert target.status == "active"
    assert target.content == CURRENT
    async with pg_factory() as session:
        provenance = (await session.execute(
            select(MemoryProvenanceRow).where(
                MemoryProvenanceRow.memory_id == seeded.target_id,
                MemoryProvenanceRow.relationship == "owner_conflict_kept_current",
            )
        )).scalar_one()
        assert provenance.source_candidate_id == seeded.candidate_id
        assert (await session.execute(
            select(func.count()).select_from(MemoryCommandRow)
        )).scalar_one() == 0
    assert await _search_conflicts(pg_factory) == []
    again = await lifecycle.resolve_obligation({
        "obligation_id": str(seeded.obligation_id), "decision": "keep_current",
        "review_version": item["review_version"],
    })
    assert again["result"] == "already_resolved"


@pytest.mark.asyncio
async def test_accept_competing_queues_explicit_correction_and_publishes(pg_factory):
    seeded = await _raise_conflict(pg_factory)
    lifecycle = MemoryLifecycleCommands(pg_factory)
    item = await _listed_conflict(lifecycle, seeded.obligation_id)
    resolved = await lifecycle.resolve_obligation({
        "obligation_id": str(seeded.obligation_id),
        "decision": "accept_competing",
        "review_version": item["review_version"],
    })
    assert resolved["result"] == "queued_for_verification"
    assert resolved["status"] == "queued"
    assert resolved["decision"] == "accept_competing"
    assert resolved["_context_suppression"]["contents"] == [CURRENT]
    assert resolved["conflict_obligation_id"] == str(seeded.obligation_id)

    conflict, obligation, candidate, target = await _conflict_state(pg_factory, seeded)
    assert conflict.status == "resolved_accept_competing"
    assert obligation.resolution_code == "accept_competing"
    assert obligation.resolution_json["command_id"] == resolved["command_id"]
    assert candidate.status == "rejected"
    assert candidate.decision_json["code"] == "owner_resolved_conflict"
    assert candidate.decision_json["superseded_by_command_id"] == resolved["command_id"]
    assert target.status == "active"
    async with pg_factory() as session:
        command = await session.get(MemoryCommandRow, UUID(resolved["command_id"]))
        assert command.operation == "correct"
        assert command.status == "pending"
        assert command.target_memory_id == seeded.target_id
        assert command.arguments_json["change_type"] == "correction"
        correction = await session.get(MemoryCandidateRow, UUID(resolved["candidate_id"]))
        assert correction.origin == "explicit_owner_correction"
        assert correction.content == COMPETING
    assert await _search_conflicts(pg_factory) == []

    await _publish_ok(pg_factory)
    _, explicit_obligation, command = await _rows(pg_factory, resolved)
    assert explicit_obligation.resolution_code == "published"
    assert command.status == "applied"
    new_id = UUID(str(explicit_obligation.resolution_json["memory_id"]))
    async with pg_factory() as session:
        old = await session.get(DurableMemoryRow, seeded.target_id)
        new = await session.get(DurableMemoryRow, new_id)
        assert old.status == "superseded"
        assert old.suppresses_recall is True
        assert old.superseded_by_id == new_id
        assert new.status == "active"
        assert new.content == COMPETING
        assert new.supersedes_id == seeded.target_id
    found = await MemoryService(pg_factory).search({"query": "shift reports", "limit": 5})
    assert found["memory_conflicts"] == []
    assert [item["memory_id"] for item in found["durable_memories"]] == [str(new_id)]


@pytest.mark.asyncio
async def test_restate_uses_owner_wording_through_explicit_pipeline(pg_factory):
    seeded = await _raise_conflict(pg_factory)
    lifecycle = MemoryLifecycleCommands(pg_factory)
    item = await _listed_conflict(lifecycle, seeded.obligation_id)
    with pytest.raises(ValueError, match="restate requires content"):
        await lifecycle.resolve_obligation({
            "obligation_id": str(seeded.obligation_id), "decision": "restate",
            "review_version": item["review_version"],
        })
    with pytest.raises(ValueError, match="use keep_current"):
        await lifecycle.resolve_obligation({
            "obligation_id": str(seeded.obligation_id), "decision": "restate",
            "content": f"  {CURRENT}  ", "review_version": item["review_version"],
        })
    conflict, _, _, _ = await _conflict_state(pg_factory, seeded)
    assert conflict.status == "open"

    restated = "Jaco prefers a one-page shift summary with a detailed appendix."
    resolved = await lifecycle.resolve_obligation({
        "obligation_id": str(seeded.obligation_id), "decision": "restate",
        "content": restated, "change_type": "change_over_time",
        "review_version": item["review_version"],
    })
    assert resolved["result"] == "queued_for_verification"
    conflict, obligation, _, _ = await _conflict_state(pg_factory, seeded)
    assert conflict.status == "resolved_restate"
    assert obligation.resolution_json["change_type"] == "change_over_time"
    async with pg_factory() as session:
        correction = await session.get(MemoryCandidateRow, UUID(resolved["candidate_id"]))
        assert correction.content == restated
    await _publish_ok(pg_factory)
    _, explicit_obligation, _ = await _rows(pg_factory, resolved)
    assert explicit_obligation.resolution_code == "published"
    new_id = UUID(str(explicit_obligation.resolution_json["memory_id"]))
    async with pg_factory() as session:
        old = await session.get(DurableMemoryRow, seeded.target_id)
        new = await session.get(DurableMemoryRow, new_id)
        assert new.content == restated
        assert old.status == "superseded"
        assert old.valid_to is not None
        provenance = list((await session.execute(
            select(MemoryProvenanceRow).where(MemoryProvenanceRow.memory_id == new_id)
        )).scalars())
        assert ("supersedes", seeded.target_id) in {
            (row.relationship, row.source_memory_id) for row in provenance
        }


@pytest.mark.asyncio
async def test_conflict_resolution_rejects_stale_review_version(pg_factory):
    seeded = await _raise_conflict(pg_factory)
    lifecycle = MemoryLifecycleCommands(pg_factory)
    item = await _listed_conflict(lifecycle, seeded.obligation_id)
    async with pg_factory() as session:
        target = await session.get(DurableMemoryRow, seeded.target_id)
        target.content = "Jaco prefers detailed shift reports, printed."
        target.updated_at = datetime.now(UTC)
        await session.commit()
    stale = await lifecycle.resolve_obligation({
        "obligation_id": str(seeded.obligation_id), "decision": "accept_competing",
        "review_version": item["review_version"],
    })
    assert stale["result"] == "stale_review"
    assert stale["target_content"] == "Jaco prefers detailed shift reports, printed."
    assert stale["review_version"] != item["review_version"]
    conflict, obligation, candidate, _ = await _conflict_state(pg_factory, seeded)
    assert conflict.status == "open"
    assert obligation.status == "pending"
    assert candidate.status == "awaiting_owner"
    async with pg_factory() as session:
        assert (await session.execute(
            select(func.count()).select_from(MemoryCommandRow)
        )).scalar_one() == 0
    fresh = await lifecycle.resolve_obligation({
        "obligation_id": str(seeded.obligation_id), "decision": "keep_current",
        "review_version": stale["review_version"],
    })
    assert fresh["result"] == "keep_current"


@pytest.mark.asyncio
async def test_conflict_resolution_is_atomic_when_correction_intake_fails(pg_factory, monkeypatch):
    seeded = await _raise_conflict(pg_factory)
    lifecycle = MemoryLifecycleCommands(pg_factory)
    item = await _listed_conflict(lifecycle, seeded.obligation_id)

    async def explode(*_args, **_kwargs):
        raise RuntimeError("forced intake failure inside the resolution transaction")

    monkeypatch.setattr(MemoryLifecycleCommands, "_queue_explicit_content_command_in", explode)
    with pytest.raises(RuntimeError):
        await lifecycle.resolve_obligation({
            "obligation_id": str(seeded.obligation_id), "decision": "accept_competing",
            "review_version": item["review_version"],
        })
    conflict, obligation, candidate, _ = await _conflict_state(pg_factory, seeded)
    assert conflict.status == "open"
    assert obligation.status == "pending"
    assert candidate.status == "awaiting_owner"
    async with pg_factory() as session:
        assert (await session.execute(
            select(func.count()).select_from(MemoryCommandRow)
        )).scalar_one() == 0
        assert (await session.execute(
            select(func.count()).select_from(MemoryCandidateRow)
        )).scalar_one() == 1
    assert (await _listed_conflict(lifecycle, seeded.obligation_id))["review_version"] == item["review_version"]


@pytest.mark.asyncio
async def test_expired_conflict_remains_resolvable(pg_factory):
    seeded = await _raise_conflict(pg_factory)
    async with pg_factory() as session:
        obligation = await session.get(MemoryObligationRow, seeded.obligation_id)
        obligation.expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        await session.commit()
    _, expired = await MemoryCandidateLeaseRepository(pg_factory).claim_batch(limit=4)
    assert expired == 1
    conflict, obligation, candidate, _ = await _conflict_state(pg_factory, seeded)
    assert conflict.status == "expired_unresolved"
    assert obligation.resolution_code == "expired_unconfirmed"
    assert candidate.status == "expired"
    surfaced = await _search_conflicts(pg_factory)
    assert surfaced[0]["status"] == "expired_unresolved"

    lifecycle = MemoryLifecycleCommands(pg_factory)
    listing = await lifecycle.obligations({"kind": "memory_conflict", "status": "resolved"})
    item = next(entry for entry in listing["obligations"] if entry["obligation_id"] == str(seeded.obligation_id))
    assert item["conflict_status"] == "expired_unresolved"
    assert item["review_version"] is not None
    resolved = await lifecycle.resolve_obligation({
        "obligation_id": str(seeded.obligation_id), "decision": "keep_current",
        "review_version": item["review_version"],
    })
    assert resolved["result"] == "keep_current"
    conflict, obligation, candidate, _ = await _conflict_state(pg_factory, seeded)
    assert conflict.status == "resolved_keep_current"
    assert obligation.resolution_code == "keep_current"
    assert candidate.status == "expired"
    assert await _search_conflicts(pg_factory) == []


@pytest.mark.asyncio
async def test_conflict_from_explicit_correction_settles_its_command_obligation(pg_factory):
    transcript_id, _ = await _seed_owner_turn(pg_factory, "Remember that I live in London.")
    service = MemoryService(pg_factory)
    queued = await service.remember({"content": "I live in London."})
    await _publish_ok(pg_factory)
    _, obligation, _ = await _rows(pg_factory, queued)
    target_id = UUID(str(obligation.resolution_json["memory_id"]))
    await _append_owner_turn(pg_factory, transcript_id, "Actually, I live in Lisbon.")
    corrected = await service.correct({"memory_id": str(target_id), "content": "I live in Lisbon."})
    # The verifier disputes the correction instead of superseding: a conflict is raised.
    disputing = _OwnerClaimModel(pg_factory, reconcile=lambda candidate: {
        "relation": "conflicts_with", "target_memory_id": str(target_id),
        "replacement_content": candidate.content,
    })
    assert (await _service(pg_factory, disputing).run_once()).blocked == 1
    candidate, explicit_obligation, command = await _rows(pg_factory, corrected)
    assert candidate.status == "awaiting_owner"
    assert explicit_obligation.status == "pending"
    assert command.status == "pending"

    lifecycle = MemoryLifecycleCommands(pg_factory)
    async with pg_factory() as session:
        conflict_obligation = (await session.execute(
            select(MemoryObligationRow).where(MemoryObligationRow.kind == "memory_conflict")
        )).scalar_one()
    item = await _listed_conflict(lifecycle, conflict_obligation.id)
    assert item["candidate_origin"] == "explicit_owner_correction"
    resolved = await lifecycle.resolve_obligation({
        "obligation_id": str(conflict_obligation.id), "decision": "keep_current",
        "review_version": item["review_version"],
    })
    assert resolved["result"] == "keep_current"
    candidate, explicit_obligation, command = await _rows(pg_factory, corrected)
    assert candidate.status == "rejected"
    assert explicit_obligation.status == "resolved"
    assert explicit_obligation.resolution_code == "rejected"
    assert explicit_obligation.resolution_json["code"] == "owner_kept_current"
    assert command.status == "failed"
    async with pg_factory() as session:
        target = await session.get(DurableMemoryRow, target_id)
        assert target.status == "active"
        assert target.content == "I live in London."


@pytest.mark.asyncio
async def test_pending_attention_renders_conflicts_and_review_backlog_bounded(pg_factory):
    seeded = await _raise_conflict(pg_factory)
    async with pg_factory() as session:
        for index in range(7):
            memory = DurableMemoryRow(
                status="active", record_kind="owner_directed", origin="legacy_pre25a13",
                grounding_status="legacy_unverified", scope="cross_chat", durability="long_term",
                content=f"Legacy fact number {index} about the workshop.",
                fingerprint=f"{index:064x}", suppresses_recall=False,
            )
            session.add(memory)
            await session.flush()
            session.add(MemoryObligationRow(
                kind="memory_review", status="pending", subject_type="durable_memory",
                subject_id=memory.id, origin="legacy_pre25a13",
            ))
        await session.commit()
    lifecycle = MemoryLifecycleCommands(pg_factory)
    attention = await lifecycle.pending_owner_attention(seeded.transcript_id)
    assert len(attention["conflicts"]) == 1
    conflict = attention["conflicts"][0]
    assert conflict["obligation_id"] == str(seeded.obligation_id)
    assert conflict["target_content"] == CURRENT
    assert conflict["competing_claim"] == COMPETING
    assert conflict["raised_in_this_chat"] is True
    assert attention["review_backlog"] == 7
    assert len(attention["reviews"]) == 5
    assert all(item["text"].startswith("Legacy fact number") for item in attention["reviews"])
    elsewhere = await lifecycle.pending_owner_attention(None)
    assert elsewhere["conflicts"][0]["raised_in_this_chat"] is False

    rendered = render_memory_attention(attention)
    assert rendered is not None
    assert len(rendered) <= 1_200
    assert str(seeded.obligation_id) in rendered
    assert "[raised in this chat]" in rendered
    assert "7 legacy memories await owner review" in rendered
    assert rendered.count("review [obligation_id=") == 5
    assert render_memory_attention({"conflicts": [], "review_backlog": 0, "reviews": []}) is None

    messages = turns_to_provider_messages(
        [], memory_attention=rendered, suppressed_contents=[CURRENT],
    )
    assert messages[0]["role"] == "developer"
    assert "Atlas pending owner memory attention" in messages[0]["content"]
    assert CURRENT not in messages[0]["content"]
    assert COMPETING in messages[0]["content"]

    unbounded = render_memory_attention({
        "conflicts": [
            {"obligation_id": f"{index:032x}", "target_content": "x" * 160,
             "competing_claim": "y" * 160, "reason_code": None}
            for index in range(5)
        ],
        "review_backlog": 0,
        "reviews": [],
    })
    assert unbounded is not None and len(unbounded) <= 1_200


@pytest.mark.asyncio
async def test_conflict_clarification_round_trip_through_run_executor(pg_factory, tmp_path):
    seeded = await _raise_conflict(pg_factory)
    memory = MemoryService(pg_factory)
    lifecycle = MemoryLifecycleCommands(pg_factory)
    runtime = CapabilityRuntime(build_phase0_registry().operations())
    runtime.register_executor("memory.search", memory.search)
    runtime.register_executor("memory.obligations.list", memory.obligations)
    runtime.register_executor("memory.obligations.resolve", memory.resolve_obligation)

    async def policy():
        return {"atlas.memory"}

    runtime.policy_reader = policy

    async def start_run() -> RunExecutor:
        async with pg_factory() as session:
            run_id = await AuthorityStore(session).create_run(
                transcript_id=seeded.transcript_id, intent="fixture"
            )
            await session.commit()
        return RunExecutor(
            pg_factory, runtime, ArtifactStore(tmp_path / "artifacts"),
            run_id=run_id, transcript_id=seeded.transcript_id,
        )

    def output(result: dict) -> dict:
        payload = result.get("output") if isinstance(result, dict) else None
        return payload if isinstance(payload, dict) else result

    # Turn one: the attention message names the conflict; the model lists it and asks.
    rendered = render_memory_attention(await lifecycle.pending_owner_attention(seeded.transcript_id))
    developer = turns_to_provider_messages([], memory_attention=rendered)[0]
    assert developer["role"] == "developer"
    assert str(seeded.obligation_id) in developer["content"]
    assert CURRENT in developer["content"] and COMPETING in developer["content"]

    first = await start_run()
    listed = output(await first.tool_handler("atlas_capability_call", {
        "operation_id": "memory.obligations.list",
        "arguments": {"kind": "memory_conflict", "status": "pending"},
    }))
    item = next(
        entry for entry in listed["obligations"]
        if entry["obligation_id"] == str(seeded.obligation_id)
    )
    assert item["competing_claim"] == COMPETING
    review_version = item["review_version"]
    clarification = (
        f'I have "{CURRENT}" as current but a later claim says "{COMPETING}". '
        "Which is right, or how should I word it?"
    )
    await _append_atlas_turn(pg_factory, seeded.transcript_id, clarification)
    async with pg_factory() as session:
        await AuthorityStore(session).finish_run(first.run_id)
        await session.commit()

    # Turn two: the owner answers with the correct wording; the model resolves it.
    restated = "Jaco prefers compact shift reports with a detailed appendix."
    answer_turn_id = await _append_owner_turn(
        pg_factory, seeded.transcript_id, f"Neither exactly. Use: {restated}"
    )
    second = await start_run()
    resolved = output(await second.tool_handler("atlas_capability_call", {
        "operation_id": "memory.obligations.resolve",
        "arguments": {
            "obligation_id": str(seeded.obligation_id),
            "decision": "restate",
            "content": restated,
            "review_version": review_version,
        },
    }))
    assert resolved["result"] == "queued_for_verification", resolved
    # The provider loop strips this transient hint before the model sees the result.
    assert resolved["_context_suppression"]["contents"] == [CURRENT]
    async with pg_factory() as session:
        command = await session.get(MemoryCommandRow, UUID(resolved["command_id"]))
        # The correction is grounded on the owner's answer in this chat.
        assert command.source_turn_id == answer_turn_id
        assert command.source_transcript_id == seeded.transcript_id
    rejected_twice = output(await second.tool_handler("atlas_capability_call", {
        "operation_id": "memory.obligations.resolve",
        "arguments": {"obligation_id": str(seeded.obligation_id), "decision": "keep_current",
                      "review_version": review_version},
    }))
    assert rejected_twice["result"] == "already_resolved"
    invalid = await second.tool_handler("atlas_capability_call", {
        "operation_id": "memory.obligations.resolve",
        "arguments": {"obligation_id": str(seeded.obligation_id), "decision": "overrule"},
    })
    assert "decision" in json.dumps(invalid)

    await _publish_ok(pg_factory)
    found = output(await second.tool_handler("atlas_capability_call", {
        "operation_id": "memory.search",
        "arguments": {"query": "shift reports", "limit": 5},
    }))
    assert found["memory_conflicts"] == []
    assert [entry["content"] for entry in found["durable_memories"]] == [restated]
    assert render_memory_attention(await lifecycle.pending_owner_attention(seeded.transcript_id)) is None


async def _append_atlas_turn(pg_factory, transcript_id: UUID, text: str) -> UUID:
    from atlas.persistence.models import TranscriptRow

    async with pg_factory() as session:
        transcript = await session.get(TranscriptRow, transcript_id)
        sequence = int(transcript.next_turn_sequence or 0) + 1
        transcript.next_turn_sequence = sequence
        turn = TurnRow(
            transcript_id=transcript_id, sequence=sequence, actor="atlas",
            blocks=[{"type": "text", "text": text}],
        )
        session.add(turn)
        await session.commit()
        return turn.id
