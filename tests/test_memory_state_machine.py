from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from atlas.memory.candidates import MemoryCandidateIntake
from atlas.memory.durable import DurableMemoryRepository
from atlas.memory.lifecycle import MemoryLifecycleCommands
from atlas.memory.observability import MemoryObservabilityService
from atlas.memory.reconciliation import (
    MemoryCandidateLeaseRepository,
    MemoryReconciliationService,
)
from atlas.memory.service import MemoryService
from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryComparisonVerdictRow,
    MemoryConflictRow,
    MemoryIndependentReadingRow,
    MemoryObligationRow,
    MemoryReconciliationRecordRow,
    SharedResourceVersionRow,
    TranscriptRow,
    TurnRow,
)
from sqlalchemy import func, select


async def _seed(
    pg_factory,
    *,
    content: str,
    kind: str = "preference",
    scope: str = "cross_chat",
    durability: str = "long_term",
    created_at: datetime | None = None,
    intake_path: str = "foreground",
):
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id,
            sequence=1,
            actor="owner",
            blocks=[{"type": "text", "text": content}],
            created_at=created_at or datetime.now(UTC),
        )
        session.add(turn)
        await session.commit()
        transcript_id, turn_id = transcript.id, turn.id
    raw = {
        "kind": kind,
        "content": content,
        "scope": scope,
        "confidence": 0.9,
        "durability": durability,
        "proposed_action": "upsert",
        "subject": "Jaco",
        "evidence_refs": [{"turn_id": str(turn_id), "span_ref": "text:0"}],
    }
    result = await MemoryCandidateIntake(pg_factory).enqueue_many(
        [raw],
        source_transcript_id=transcript_id,
        source_turn_id=turn_id,
        source_provider_evidence_id=None,
        proposer_model="foreground-test",
        intake_path=intake_path,
    )
    assert result["accepted"] == 1
    async with pg_factory() as session:
        candidate = (
            await session.execute(
                select(MemoryCandidateRow).where(MemoryCandidateRow.source_turn_id == turn_id)
            )
        ).scalar_one()
    return candidate.id, transcript_id, turn_id, raw


class _PipelineModel:
    model = "verifier-test"

    def __init__(
        self,
        *,
        content: str,
        relation: str = "new",
        target_memory_id: UUID | None = None,
        event_valid_from: datetime | None = None,
        category: str = "preference",
    ) -> None:
        self.content = content
        self.relation = relation
        self.target_memory_id = target_memory_id
        self.event_valid_from = event_valid_from
        self.category = category
    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str:
        if "blind evidence-reading stage" in instructions:
            return json.dumps({
                "extracted_claims": [self.content],
                "category": self.category,
                "scope": "cross_chat",
                "durability": "long_term",
                "event_valid_from": (
                    self.event_valid_from.isoformat() if self.event_valid_from else None
                ),
                "event_valid_to": None,
            })
        if "Compare a foreground memory proposal" in instructions:
            return json.dumps({
                "verdict": "agree",
                "normalized_content": self.content,
                "category": self.category,
                "scope": "cross_chat",
                "durability": "long_term",
            })
        if "Reconcile one independently verified claim" in instructions:
            return json.dumps({
                "relation": self.relation,
                "target_memory_id": str(self.target_memory_id) if self.target_memory_id else None,
                "replacement_content": (
                    self.content if self.relation != "new" else None
                ),
            })
        raise AssertionError("unexpected model stage")


class _BlindCommitModel(_PipelineModel):
    def __init__(self, factory, **kwargs) -> None:
        super().__init__(**kwargs)
        self.factory = factory
        self.saw_persisted_reading = False

    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str:
        if "Compare a foreground memory proposal" in instructions:
            async with self.factory() as session:
                count = (
                    await session.execute(
                        select(func.count()).select_from(MemoryIndependentReadingRow)
                    )
                ).scalar_one()
                self.saw_persisted_reading = count == 1
                assert self.saw_persisted_reading
        return await super().complete_text(instructions=instructions, messages=messages)


@pytest.mark.asyncio
async def test_blind_reading_is_persisted_before_proposal_exposure(pg_factory):
    content = "Jaco prefers fresh topic chats."
    await _seed(pg_factory, content=content)
    model = _BlindCommitModel(pg_factory, content=content)
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.reconciled == 1
    assert model.saw_persisted_reading is True
    async with pg_factory() as session:
        reading = (await session.execute(select(MemoryIndependentReadingRow))).scalar_one()
        comparison = (await session.execute(select(MemoryComparisonVerdictRow))).scalar_one()
        assert comparison.reading_id == reading.id
        assert reading.verifier_model == "verifier-test"


class _CASBumpModel(_PipelineModel):
    def __init__(self, factory, **kwargs) -> None:
        super().__init__(**kwargs)
        self.factory = factory
        self.calls = {"blind": 0, "compare": 0, "reconcile": 0}
        self.bumped = False

    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str:
        if "blind evidence-reading stage" in instructions:
            self.calls["blind"] += 1
        elif "Compare a foreground memory proposal" in instructions:
            self.calls["compare"] += 1
        elif "Reconcile one independently verified claim" in instructions:
            self.calls["reconcile"] += 1
            if not self.bumped:
                async with self.factory() as session:
                    session.add(SharedResourceVersionRow(
                        resource_type="memory_state", resource_id="owner", version=1
                    ))
                    await session.commit()
                self.bumped = True
        return await super().complete_text(instructions=instructions, messages=messages)


@pytest.mark.asyncio
async def test_cas_conflict_reconciles_again_without_rerunning_blind_read(pg_factory):
    content = "Jaco prefers local-first tools."
    await _seed(pg_factory, content=content)
    model = _CASBumpModel(pg_factory, content=content)
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.reconciled == 1
    assert result.version_conflicts == 1
    assert model.calls == {"blind": 1, "compare": 1, "reconcile": 2}
    async with pg_factory() as session:
        assert (
            await session.execute(
                select(func.count()).select_from(MemoryIndependentReadingRow)
            )
        ).scalar_one() == 1
        assert (
            await session.execute(
                select(func.count()).select_from(MemoryComparisonVerdictRow)
            )
        ).scalar_one() == 1
        records = list(
            (
                await session.execute(
                    select(MemoryReconciliationRecordRow).order_by(
                        MemoryReconciliationRecordRow.created_at
                    )
                )
            ).scalars()
        )
        assert [row.evaluated_memory_revision for row in records] == [0, 1]


class _PurgeDuringBlindModel(_PipelineModel):
    def __init__(self, factory, memory_id: UUID, **kwargs) -> None:
        super().__init__(**kwargs)
        self.factory = factory
        self.memory_id = memory_id
        self.purged = False

    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str:
        if "blind evidence-reading stage" in instructions and not self.purged:
            command = MemoryLifecycleCommands(self.factory)
            await command.delete({
                "memory_id": str(self.memory_id), "scope": "memory_and_sources"
            })
            self.purged = True
        return await super().complete_text(instructions=instructions, messages=messages)


@pytest.mark.asyncio
async def test_purge_while_blind_verifier_runs_aborts_candidate(pg_factory, caplog):
    content = "Jaco prefers the temporary starling setting."
    candidate_id, transcript_id, turn_id, _ = await _seed(pg_factory, content=content)
    async with pg_factory() as session:
        memory, _ = await DurableMemoryRepository(session).create_active(
            content,
            source_transcript_id=transcript_id,
            source_turn_id=turn_id,
            record_kind="owner_directed",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
        )
        await session.commit()
        memory_id = memory.id
    model = _PurgeDuringBlindModel(pg_factory, memory_id, content=content)
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.failures == 1
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        memory = await session.get(DurableMemoryRow, memory_id)
        assert candidate.status == "invalidated"
        assert candidate.content is None
        assert memory.status == "deleted"
        assert (
            await session.execute(
                select(func.count()).select_from(MemoryIndependentReadingRow)
            )
        ).scalar_one() == 0
        detail = await MemoryObservabilityService(session).candidate_detail(candidate_id)
        assert content not in json.dumps(detail, default=str)
    assert content not in caplog.text


class _PurgeDuringReconcileModel(_PipelineModel):
    def __init__(self, factory, memory_id: UUID, **kwargs) -> None:
        super().__init__(**kwargs)
        self.factory = factory
        self.memory_id = memory_id
        self.purged = False

    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str:
        if "Reconcile one independently verified claim" in instructions and not self.purged:
            command = MemoryLifecycleCommands(self.factory)
            await command.delete({
                "memory_id": str(self.memory_id), "scope": "memory_and_sources"
            })
            self.purged = True
        return await super().complete_text(instructions=instructions, messages=messages)


@pytest.mark.asyncio
async def test_purge_after_blind_read_tombstones_semantic_artifacts(pg_factory):
    content = "Jaco prefers a transient dashboard mode."
    candidate_id, transcript_id, turn_id, _ = await _seed(pg_factory, content=content)
    async with pg_factory() as session:
        memory, _ = await DurableMemoryRepository(session).create_active(
            content,
            source_transcript_id=transcript_id,
            source_turn_id=turn_id,
            record_kind="owner_directed",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
        )
        await session.commit()
        memory_id = memory.id
    model = _PurgeDuringReconcileModel(
        pg_factory, memory_id, content=content, relation="conflicts_with",
        target_memory_id=memory_id,
    )
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.failures == 1
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        reading = (await session.execute(select(MemoryIndependentReadingRow))).scalar_one()
        comparison = (await session.execute(select(MemoryComparisonVerdictRow))).scalar_one()
        assert candidate.status == "invalidated"
        assert reading.tombstoned_at is not None
        assert reading.extracted_claims_json == []
        assert reading.category is None
        assert comparison.tombstoned_at is not None
        assert comparison.normalized_content is None
        assert comparison.category is None


@pytest.mark.asyncio
async def test_older_event_discovered_late_becomes_historical_predecessor(pg_factory):
    old_time = datetime(2026, 8, 1, tzinfo=UTC)
    new_time = datetime(2026, 9, 1, tzinfo=UTC)
    old_content = "Jaco prefers compact reports."
    candidate_id, _, _, _ = await _seed(
        pg_factory, content=old_content, created_at=old_time
    )
    async with pg_factory() as session:
        target, _ = await DurableMemoryRepository(session).create_active(
            "Jaco now prefers detailed reports.",
            source_transcript_id=None,
            source_turn_id=None,
            record_kind="derived",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
            valid_from=new_time,
        )
        await session.commit()
        target_id = target.id
    model = _PipelineModel(
        content=old_content,
        relation="supersedes",
        target_memory_id=target_id,
        event_valid_from=old_time,
    )
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.reconciled == 1
    async with pg_factory() as session:
        target = await session.get(DurableMemoryRow, target_id)
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        historical = (
            await session.execute(
                select(DurableMemoryRow).where(
                    DurableMemoryRow.id != target_id,
                    DurableMemoryRow.status == "superseded",
                )
            )
        ).scalar_one()
        assert target.status == "active"
        assert historical.content == old_content
        assert historical.superseded_by_id == target_id
        assert historical.valid_from == old_time
        assert historical.valid_to == new_time
        assert candidate.decision_json["decision"] == "historical_predecessor"


class _CurrentTargetModel(_PipelineModel):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.calls = {"blind": 0, "compare": 0, "reconcile": 0}
        self.targets: list[UUID] = []

    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str:
        if "blind evidence-reading stage" in instructions:
            self.calls["blind"] += 1
        elif "Compare a foreground memory proposal" in instructions:
            self.calls["compare"] += 1
        elif "Reconcile one independently verified claim" in instructions:
            self.calls["reconcile"] += 1
            payload = json.loads(messages[0]["content"])
            target_id = UUID(payload["applicable_active_memories"][0]["memory_id"])
            self.targets.append(target_id)
            return json.dumps({
                "relation": "duplicate_of",
                "target_memory_id": str(target_id),
                "replacement_content": None,
            })
        return await super().complete_text(instructions=instructions, messages=messages)


@pytest.mark.asyncio
async def test_owner_confirmation_reconciles_again_against_current_graph(pg_factory):
    content = "Jaco prefers a private health reminder format."
    candidate_id, _, turn_id, _ = await _seed(pg_factory, content=content)
    async with pg_factory() as session:
        old_target, _ = await DurableMemoryRepository(session).create_active(
            content,
            source_transcript_id=None,
            source_turn_id=None,
            record_kind="derived",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
        )
        await session.commit()
        old_target_id = old_target.id
    model = _CurrentTargetModel(content=content, category="health")
    first = await MemoryReconciliationService(pg_factory, model).run_once()
    assert first.blocked == 1
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        obligation = (
            await session.execute(
                select(MemoryObligationRow).where(
                    MemoryObligationRow.subject_id == candidate_id,
                    MemoryObligationRow.kind == "memory_confirmation",
                )
            )
        ).scalar_one()
        assert candidate.status == "awaiting_owner"
        obligation_id = obligation.id
        old_target = await session.get(DurableMemoryRow, old_target_id)
        old_target.status = "superseded"
        old_target.suppresses_recall = True
        replacement, created = await DurableMemoryRepository(session).create_active(
            content,
            source_transcript_id=None,
            source_turn_id=None,
            record_kind="derived",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
        )
        assert created is True
        version = await session.get(
            SharedResourceVersionRow,
            {"resource_type": "memory_state", "resource_id": "owner"},
        )
        version.version = int(version.version) + 1
        await session.commit()
        replacement_id = replacement.id
    resolved = await MemoryLifecycleCommands(pg_factory).resolve_obligation({
        "obligation_id": str(obligation_id), "decision": "confirm"
    })
    assert resolved["result"] == "requeued_for_reconciliation"
    second = await MemoryReconciliationService(pg_factory, model).run_once()
    assert second.reconciled == 1
    assert model.calls == {"blind": 1, "compare": 1, "reconcile": 2}
    assert model.targets == [old_target_id, replacement_id]
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        obligation = await session.get(MemoryObligationRow, obligation_id)
        assert candidate.status == "reconciled"
        assert obligation.status == "resolved"
        assert obligation.resolution_code == "confirmed"
        assert obligation.resolution_source_turn_id == turn_id
        assert (
            await session.execute(
                select(func.count()).select_from(MemoryIndependentReadingRow)
            )
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_owner_rejection_terminates_confirmation_obligation(pg_factory):
    content = "Jaco prefers a private health reminder format."
    candidate_id, _, _, _ = await _seed(pg_factory, content=content)
    model = _PipelineModel(content=content, category="health")
    first = await MemoryReconciliationService(pg_factory, model).run_once()
    assert first.blocked == 1
    async with pg_factory() as session:
        obligation = (
            await session.execute(
                select(MemoryObligationRow).where(
                    MemoryObligationRow.subject_id == candidate_id,
                    MemoryObligationRow.kind == "memory_confirmation",
                )
            )
        ).scalar_one()
        obligation_id = obligation.id
    resolved = await MemoryLifecycleCommands(pg_factory).resolve_obligation({
        "obligation_id": str(obligation_id), "decision": "reject"
    })
    assert resolved["result"] == "rejected"
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        obligation = await session.get(MemoryObligationRow, obligation_id)
        assert candidate.status == "rejected"
        assert obligation.status == "resolved"
        assert obligation.resolution_code == "rejected"
    again = await MemoryReconciliationService(pg_factory, model).run_once()
    assert again.claimed == 0


@pytest.mark.asyncio
async def test_owner_confirmation_obligation_expires_visibly(pg_factory):
    content = "Jaco prefers a private health reminder format."
    candidate_id, _, _, _ = await _seed(pg_factory, content=content)
    model = _PipelineModel(content=content, category="health")
    await MemoryReconciliationService(pg_factory, model).run_once()
    async with pg_factory() as session:
        obligation = (
            await session.execute(
                select(MemoryObligationRow).where(
                    MemoryObligationRow.subject_id == candidate_id
                )
            )
        ).scalar_one()
        obligation.expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        obligation_id = obligation.id
        await session.commit()
    claims, expired = await MemoryCandidateLeaseRepository(pg_factory).claim_batch(limit=4)
    assert claims == []
    assert expired == 1
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        obligation = await session.get(MemoryObligationRow, obligation_id)
        assert candidate.status == "expired"
        assert obligation.status == "resolved"
        assert obligation.resolution_code == "expired_unconfirmed"


@pytest.mark.asyncio
async def test_conflict_is_visible_in_retrieval_and_expiry_does_not_hide_it(pg_factory):
    current = "Jaco prefers detailed shift reports."
    competing = "Jaco prefers compact shift reports."
    candidate_id, _, _, _ = await _seed(pg_factory, content=competing)
    async with pg_factory() as session:
        target, _ = await DurableMemoryRepository(session).create_active(
            current,
            source_transcript_id=None,
            source_turn_id=None,
            record_kind="derived",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
        )
        await session.commit()
        target_id = target.id
    model = _PipelineModel(
        content=competing,
        relation="conflicts_with",
        target_memory_id=target_id,
    )
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.blocked == 1
    search = await MemoryService(pg_factory).search({"query": "shift reports", "limit": 5})
    assert len(search["memory_conflicts"]) == 1
    conflict = search["memory_conflicts"][0]
    assert conflict["target_memory_id"] == str(target_id)
    assert conflict["competing_claim"] == competing
    assert "neither" in conflict["retrieval_instruction"].casefold()
    async with pg_factory() as session:
        obligation = (
            await session.execute(
                select(MemoryObligationRow).where(
                    MemoryObligationRow.subject_type == "memory_conflict"
                )
            )
        ).scalar_one()
        obligation.expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        conflict_id = obligation.subject_id
        await session.commit()
    _, expired = await MemoryCandidateLeaseRepository(pg_factory).claim_batch(limit=4)
    assert expired == 1
    search = await MemoryService(pg_factory).search({"query": "shift reports", "limit": 5})
    assert len(search["memory_conflicts"]) == 1
    assert search["memory_conflicts"][0]["status"] == "expired_unresolved"
    async with pg_factory() as session:
        row = await session.get(MemoryConflictRow, conflict_id)
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        assert row.status == "expired_unresolved"
        assert candidate.status == "expired"


class _NewOrDuplicateModel(_PipelineModel):
    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str:
        if "Reconcile one independently verified claim" in instructions:
            payload = json.loads(messages[0]["content"])
            active = payload["applicable_active_memories"]
            if active:
                return json.dumps({
                    "relation": "duplicate_of",
                    "target_memory_id": active[0]["memory_id"],
                    "replacement_content": None,
                })
            return json.dumps({
                "relation": "new",
                "target_memory_id": None,
                "replacement_content": None,
            })
        return await super().complete_text(instructions=instructions, messages=messages)


@pytest.mark.asyncio
async def test_foreground_and_sweep_same_fact_converge_at_reconciliation(pg_factory):
    content = "Jaco prefers concise deployment summaries."
    first_id, first_transcript_id, _, _ = await _seed(
        pg_factory, content=content, intake_path="foreground"
    )
    async with pg_factory() as session:
        transcript = await session.get(TranscriptRow, first_transcript_id)
        transcript.closed_at = datetime.now(UTC)
        await session.commit()
    second_id, _, _, _ = await _seed(
        pg_factory, content=content, intake_path="sweep"
    )
    assert first_id != second_id
    model = _NewOrDuplicateModel(content=content)
    result = await MemoryReconciliationService(
        pg_factory, model, batch_size=8
    ).run_once()
    assert result.reconciled == 2
    async with pg_factory() as session:
        memories = list((await session.execute(
            select(DurableMemoryRow).where(DurableMemoryRow.status == "active")
        )).scalars())
        candidates = list((await session.execute(
            select(MemoryCandidateRow).order_by(MemoryCandidateRow.created_at)
        )).scalars())
        assert len(memories) == 1
        assert [row.status for row in candidates] == ["reconciled", "reconciled"]
        assert {row.intake_path for row in candidates} == {"foreground", "sweep"}


@pytest.mark.asyncio
async def test_legacy_unverified_memory_is_excluded_until_grounded(pg_factory):
    legacy_content = "Jaco likes grounded evidence before durable recall."
    verified_content = "Jaco prefers grounded evidence before durable recall."
    async with pg_factory() as session:
        memory, _ = await DurableMemoryRepository(session).create_active(
            legacy_content,
            source_transcript_id=None,
            source_turn_id=None,
            record_kind="derived",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
        )
        await session.commit()
        memory_id = memory.id

    before = await MemoryService(pg_factory).search({
        "query": "grounded evidence", "limit": 5
    })
    assert all(item["memory_id"] != str(memory_id) for item in before["durable_memories"])

    candidate_id, _transcript_id, turn_id, _ = await _seed(
        pg_factory, content=verified_content
    )
    result = await MemoryReconciliationService(
        pg_factory,
        _PipelineModel(
            content=verified_content,
            relation="grounds_legacy",
            target_memory_id=memory_id,
        ),
    ).run_once()
    assert result.reconciled == 1

    async with pg_factory() as session:
        memory = await session.get(DurableMemoryRow, memory_id)
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        assert memory.grounding_status == "verified"
        assert memory.content == verified_content
        assert memory.source_turn_id == turn_id
        assert candidate.status == "reconciled"

    after = await MemoryService(pg_factory).search({
        "query": "grounded evidence", "limit": 5
    })
    assert any(item["memory_id"] == str(memory_id) for item in after["durable_memories"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate_status",
    [
        "pending", "leased", "retained_short_term", "awaiting_owner",
        "reconciled", "discarded", "blocked", "invalidated",
        "expired", "failed", "rejected",
    ],
)
async def test_purge_invalidates_candidate_from_every_persisted_state(
    pg_factory, candidate_status
):
    content = f"Purge coverage for {candidate_status}."
    candidate_id, transcript_id, turn_id, _ = await _seed(pg_factory, content=content)
    async with pg_factory() as session:
        memory, _ = await DurableMemoryRepository(session).create_active(
            content,
            source_transcript_id=transcript_id,
            source_turn_id=turn_id,
            record_kind="owner_directed",
            memory_kind="fact",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
        )
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        candidate.status = candidate_status
        await session.commit()
        memory_id = memory.id
    result = await MemoryLifecycleCommands(pg_factory).delete({
        "memory_id": str(memory_id),
        "scope": "memory_and_sources",
    })
    assert result["result"] == "deleted"
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        turn = await session.get(TurnRow, turn_id)
        assert candidate.status == "invalidated"
        assert candidate.content is None
        assert candidate.invalidated_at is not None
        assert turn.deleted_at is not None
