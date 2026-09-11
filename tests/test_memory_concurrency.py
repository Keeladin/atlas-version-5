from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from atlas.memory.candidates import MemoryCandidateIntake
from atlas.memory.durable import DurableMemoryRepository, MemoryMutationError
from atlas.memory.lifecycle import MemoryLifecycleCommands
from atlas.memory.reconciliation import (
    MemoryCandidateLeaseRepository,
    MemoryReconciliationService,
)
from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateEvidenceRow,
    MemoryCandidateRow,
    MemoryCommandRow,
    MemoryConflictRow,
    MemoryObligationRow,
    MemoryProvenanceRow,
    MemoryReconciliationAttemptRow,
    MemoryReconciliationRecordRow,
    TranscriptRow,
    TurnRow,
)
from atlas.persistence.shared_writes import SharedWriteRepository
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError


class _Barrier:
    def __init__(self, parties: int = 2) -> None:
        self.parties = parties
        self.count = 0
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()

    async def wait(self) -> None:
        async with self.lock:
            self.count += 1
            if self.count >= self.parties:
                self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=3)


async def _assert_no_live_tombstoned_dependencies(factory) -> None:
    nonterminal = {
        "pending", "leased", "retained_short_term", "awaiting_owner"
    }
    async with factory() as session:
        candidate_count = int((await session.execute(
            select(func.count())
            .select_from(MemoryCandidateRow)
            .join(
                MemoryCandidateEvidenceRow,
                MemoryCandidateEvidenceRow.candidate_id == MemoryCandidateRow.id,
            )
            .join(TurnRow, TurnRow.id == MemoryCandidateEvidenceRow.turn_id)
            .where(
                MemoryCandidateRow.status.in_(nonterminal),
                TurnRow.deleted_at.is_not(None),
            )
        )).scalar_one() or 0)
        direct_memory_count = int((await session.execute(
            select(func.count())
            .select_from(DurableMemoryRow)
            .join(TurnRow, TurnRow.id == DurableMemoryRow.source_turn_id)
            .where(
                DurableMemoryRow.status == "active",
                TurnRow.deleted_at.is_not(None),
            )
        )).scalar_one() or 0)
        provenance_memory_count = int((await session.execute(
            select(func.count())
            .select_from(DurableMemoryRow)
            .join(
                MemoryProvenanceRow,
                MemoryProvenanceRow.memory_id == DurableMemoryRow.id,
            )
            .join(TurnRow, TurnRow.id == MemoryProvenanceRow.source_turn_id)
            .where(
                DurableMemoryRow.status == "active",
                TurnRow.deleted_at.is_not(None),
            )
        )).scalar_one() or 0)
    assert candidate_count == 0
    assert direct_memory_count == 0
    assert provenance_memory_count == 0


async def _enqueue(
    factory,
    *,
    transcript_id: UUID,
    turn_id: UUID,
    content: str,
    intake_path: str,
) -> UUID:
    result = await MemoryCandidateIntake(factory).enqueue_many(
        [{
            "kind": "preference",
            "content": content,
            "scope": "cross_chat",
            "confidence": 0.9,
            "durability": "long_term",
            "proposed_action": "upsert",
            "subject": "Jaco",
            "evidence_refs": [{"turn_id": str(turn_id), "span_ref": "text:0"}],
        }],
        source_transcript_id=transcript_id,
        source_turn_id=turn_id,
        source_provider_evidence_id=None,
        proposer_model="race-proposer",
        intake_path=intake_path,
    )
    assert result["accepted"] == 1
    async with factory() as session:
        candidate = (
            await session.execute(
                select(MemoryCandidateRow).where(
                    MemoryCandidateRow.source_turn_id == turn_id
                )
            )
        ).scalar_one()
        return candidate.id


class _DynamicModel:
    model = "race-verifier"

    def __init__(self, content: str, *, event_valid_from: datetime | None = None) -> None:
        self.content = content
        self.event_valid_from = event_valid_from

    async def reading(self) -> str:
        return json.dumps({
            "extracted_claims": [self.content],
            "category": "preference",
            "scope": "cross_chat",
            "durability": "long_term",
            "event_valid_from": (
                self.event_valid_from.isoformat() if self.event_valid_from else None
            ),
            "event_valid_to": None,
        })
    async def comparison(self) -> str:
        return json.dumps({
            "verdict": "agree",
            "normalized_content": self.content,
            "category": "preference",
            "scope": "cross_chat",
            "durability": "long_term",
        })


class _ConcurrentConvergeModel(_DynamicModel):
    def __init__(self, content: str, barrier: _Barrier) -> None:
        super().__init__(content)
        self.barrier = barrier

    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        if "blind evidence-reading stage" in instructions:
            return await self.reading()
        if "Compare a foreground memory proposal" in instructions:
            return await self.comparison()
        if "Reconcile one independently verified claim" in instructions:
            payload = json.loads(messages[0]["content"])
            active = payload["applicable_active_memories"]
            if not active:
                await self.barrier.wait()
                return json.dumps({
                    "relation": "new", "target_memory_id": None,
                    "replacement_content": None,
                })
            return json.dumps({
                "relation": "duplicate_of",
                "target_memory_id": active[0]["memory_id"],
                "replacement_content": None,
            })
        raise AssertionError("unexpected stage")


@pytest.mark.asyncio
async def test_foreground_and_sweep_converge_under_real_concurrent_transactions(pg_factory):
    content = "Jaco prefers concise deployment summaries."
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=2, content_revision=2)
        session.add(transcript)
        await session.flush()
        first = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": content}],
        )
        second = TurnRow(
            transcript_id=transcript.id, sequence=2, actor="owner",
            blocks=[{"type": "text", "text": "Concise deployment summaries suit me."}],
        )
        session.add_all([first, second])
        await session.commit()
        transcript_id, first_id, second_id = transcript.id, first.id, second.id
    await _enqueue(pg_factory, transcript_id=transcript_id, turn_id=first_id,
                   content=content, intake_path="foreground")
    await _enqueue(pg_factory, transcript_id=transcript_id, turn_id=second_id,
                   content=content, intake_path="sweep")
    barrier = _Barrier()
    model = _ConcurrentConvergeModel(content, barrier)
    left = MemoryReconciliationService(pg_factory, model, batch_size=1)
    right = MemoryReconciliationService(pg_factory, model, batch_size=1)
    results = await asyncio.gather(left.run_once(), right.run_once())
    assert sum(item.reconciled for item in results) == 2
    assert sum(item.version_conflicts for item in results) >= 1
    async with pg_factory() as session:
        memories = list((await session.execute(
            select(DurableMemoryRow).where(DurableMemoryRow.status == "active")
        )).scalars())
        candidates = list((await session.execute(
            select(MemoryCandidateRow).order_by(MemoryCandidateRow.created_at)
        )).scalars())
        assert len(memories) == 1
        assert {row.intake_path for row in candidates} == {"foreground", "sweep"}
        assert all(row.status == "reconciled" for row in candidates)
    await _assert_no_live_tombstoned_dependencies(pg_factory)


class _TimeoutBlindModel(_DynamicModel):
    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        if "blind evidence-reading stage" in instructions:
            raise TimeoutError("verifier timed out")
        raise AssertionError("timeout model should not reach later stages")


class _SimpleNewModel(_DynamicModel):
    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        if "blind evidence-reading stage" in instructions:
            return await self.reading()
        if "Compare a foreground memory proposal" in instructions:
            return await self.comparison()
        if "Reconcile one independently verified claim" in instructions:
            return json.dumps({
                "relation": "new", "target_memory_id": None,
                "replacement_content": None,
            })
        raise AssertionError("unexpected stage")


@pytest.mark.asyncio
async def test_verifier_timeout_requeues_and_next_lease_recovers(pg_factory):
    content = "Jaco prefers local-first tools."
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": content}],
        )
        session.add(turn)
        await session.commit()
        transcript_id, turn_id = transcript.id, turn.id
    candidate_id = await _enqueue(
        pg_factory, transcript_id=transcript_id, turn_id=turn_id,
        content=content, intake_path="foreground",
    )
    failed = await MemoryReconciliationService(
        pg_factory, _TimeoutBlindModel(content), max_attempts=3
    ).run_once()
    assert failed.failures == 1
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        assert candidate.status == "pending"
        assert candidate.lease_token is None
    recovered = await MemoryReconciliationService(
        pg_factory, _SimpleNewModel(content), max_attempts=3
    ).run_once()
    assert recovered.reconciled == 1
    async with pg_factory() as session:
        attempts = list((await session.execute(
            select(MemoryReconciliationAttemptRow)
            .where(MemoryReconciliationAttemptRow.candidate_id == candidate_id)
            .order_by(MemoryReconciliationAttemptRow.attempt_number)
        )).scalars())
        assert [row.status for row in attempts] == ["failed", "committed"]


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_after_worker_disappears(pg_factory):
    content = "Jaco prefers bounded evidence."
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": content}],
        )
        session.add(turn)
        await session.commit()
        transcript_id, turn_id = transcript.id, turn.id
    candidate_id = await _enqueue(
        pg_factory, transcript_id=transcript_id, turn_id=turn_id,
        content=content, intake_path="foreground",
    )
    leases = MemoryCandidateLeaseRepository(pg_factory, max_attempts=3)
    first, _ = await leases.claim_batch(limit=1)
    assert len(first) == 1
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        candidate.leased_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    second, _ = await leases.claim_batch(limit=1)
    assert len(second) == 1
    assert second[0].lease_token != first[0].lease_token
    async with pg_factory() as session:
        attempts = list((await session.execute(
            select(MemoryReconciliationAttemptRow)
            .where(MemoryReconciliationAttemptRow.candidate_id == candidate_id)
            .order_by(MemoryReconciliationAttemptRow.attempt_number)
        )).scalars())
        assert attempts[0].status == "lease_expired"
        assert attempts[0].completed_at is not None
        assert attempts[1].status == "claimed"


@pytest.mark.asyncio
async def test_failed_remember_transaction_rolls_back_memory_but_keeps_obligation_pending(
    pg_factory, monkeypatch
):
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": "Remember that I prefer strict rollback tests."}],
        )
        session.add(turn)
        await session.commit()
    commands = MemoryLifecycleCommands(pg_factory)

    async def fail_after_mutation(*_args, **_kwargs):
        raise RuntimeError("forced failure after memory mutation")

    monkeypatch.setattr(commands, "_apply_command", fail_after_mutation)
    with pytest.raises(MemoryMutationError):
        await commands.remember({
            "content": "Jaco prefers strict rollback tests.",
            "kind": "preference",
            "scope": "cross_chat",
            "durability": "long_term",
        })
    async with pg_factory() as session:
        assert (
            await session.execute(
                select(func.count()).select_from(DurableMemoryRow)
            )
        ).scalar_one() == 0
        command = (await session.execute(select(MemoryCommandRow))).scalar_one()
        obligation = (await session.execute(select(MemoryObligationRow))).scalar_one()
        assert command.status == "failed"
        assert obligation.kind == "explicit_remember"
        assert obligation.status == "pending"
        assert obligation.resolution_code is None
        assert obligation.resolved_at is None


@pytest.mark.asyncio
async def test_ephemeral_handle_binding_is_committed_at_intake(pg_factory):
    content = "Jaco prefers provenance with boring runtime plumbing."
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": content}],
        )
        session.add(turn)
        await session.commit()
        transcript_id, turn_id = transcript.id, turn.id
    handle_map = {"o1.0": (turn_id, "text:0")}
    result = await MemoryCandidateIntake(pg_factory).enqueue_many(
        [{
            "kind": "preference",
            "content": content,
            "scope": "cross_chat",
            "confidence": 0.9,
            "durability": "long_term",
            "proposed_action": "upsert",
            "subject": "Jaco",
            "evidence_refs": [{"handle": "o1.0"}],
        }],
        source_transcript_id=transcript_id,
        source_turn_id=turn_id,
        source_provider_evidence_id=None,
        evidence_handle_map=handle_map,
        proposer_model="foreground-test",
    )
    assert result["accepted"] == 1
    handle_map.clear()
    async with pg_factory() as session:
        evidence = (
            await session.execute(select(MemoryCandidateEvidenceRow))
        ).scalar_one()
        assert evidence.turn_id == turn_id
        assert evidence.span_ref == "text:0"
        assert evidence.principal == "owner"
    reconciled = await MemoryReconciliationService(
        pg_factory, _SimpleNewModel(content)
    ).run_once()
    assert reconciled.reconciled == 1


class _SupersedeModel(_DynamicModel):
    def __init__(self, content: str, target_id: UUID, event_valid_from: datetime) -> None:
        super().__init__(content, event_valid_from=event_valid_from)
        self.target_id = target_id

    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        if "blind evidence-reading stage" in instructions:
            return await self.reading()
        if "Compare a foreground memory proposal" in instructions:
            return await self.comparison()
        if "Reconcile one independently verified claim" in instructions:
            return json.dumps({
                "relation": "supersedes",
                "target_memory_id": str(self.target_id),
                "replacement_content": self.content,
            })
        raise AssertionError("unexpected stage")


@pytest.mark.asyncio
async def test_model_event_time_beyond_cited_evidence_horizon_cannot_supersede(pg_factory):
    observed_at = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    target_time = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)
    invented_time = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    content = "Jaco prefers detailed reports."
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": content}], created_at=observed_at,
        )
        session.add(turn)
        target, _ = await DurableMemoryRepository(session).create_active(
            "Jaco prefers compact reports.",
            source_transcript_id=None, source_turn_id=None,
            record_kind="derived", memory_kind="preference", scope="cross_chat",
            scope_key="owner", durability="long_term", subject="Jaco",
            valid_from=target_time,
        )
        await session.commit()
        transcript_id, turn_id, target_id = transcript.id, turn.id, target.id
    candidate_id = await _enqueue(
        pg_factory, transcript_id=transcript_id, turn_id=turn_id,
        content=content, intake_path="foreground",
    )
    result = await MemoryReconciliationService(
        pg_factory, _SupersedeModel(content, target_id, invented_time)
    ).run_once()
    assert result.blocked == 1
    async with pg_factory() as session:
        target = await session.get(DurableMemoryRow, target_id)
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        record = (
            await session.execute(
                select(MemoryReconciliationRecordRow)
                .where(MemoryReconciliationRecordRow.candidate_id == candidate_id)
                .order_by(MemoryReconciliationRecordRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        assert target.status == "active"
        assert candidate.status == "awaiting_owner"
        assert record.relation == "conflicts_with"
        assert record.temporal_guard == "beyond_evidence_horizon"


@pytest.mark.asyncio
async def test_purge_during_publication_commit_window_cannot_leave_live_derived_memory(
    pg_factory, monkeypatch
):
    turn_text = "Codename Falcon. Jaco prefers concise reports."
    candidate_content = "Jaco prefers concise reports."
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": turn_text}],
        )
        session.add(turn)
        await session.flush()
        owner_memory, _ = await DurableMemoryRepository(session).create_active(
            "Codename Falcon.", source_transcript_id=transcript.id,
            source_turn_id=turn.id, record_kind="owner_directed", memory_kind="fact",
            scope="cross_chat", scope_key="owner", durability="long_term",
        )
        await session.commit()
        transcript_id, turn_id, owner_memory_id = transcript.id, turn.id, owner_memory.id
    candidate_id = await _enqueue(
        pg_factory, transcript_id=transcript_id, turn_id=turn_id,
        content=candidate_content, intake_path="foreground",
    )
    service = MemoryReconciliationService(pg_factory, _SimpleNewModel(candidate_content))
    entered = asyncio.Event()
    release = asyncio.Event()
    original = service.publisher._live_evidence_rows

    async def blocked_live_evidence(session, candidate):
        rows = await original(session, candidate)
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=3)
        return rows

    monkeypatch.setattr(service.publisher, "_live_evidence_rows", blocked_live_evidence)
    publish_task = asyncio.create_task(service.run_once())
    await asyncio.wait_for(entered.wait(), timeout=3)

    # Force the adverse ordering. Publication already owns the memory resource
    # lock. The purge transaction executes a real query first (which would pin a
    # REPEATABLE READ snapshot), then blocks trying to acquire the same resource
    # lock. Only after PostgreSQL reports that wait do we allow publication to
    # commit. READ COMMITTED must then let the purge cascade see the new memory.
    original_shared_execute = SharedWriteRepository.execute
    purge_entered = asyncio.Event()
    purge_probe: dict[str, object] = {}

    async def observed_shared_execute(repository, envelope, mutation):
        if envelope.operation == "delete":
            purge_probe["pid"] = (
                await repository.session.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
            purge_probe["isolation"] = (
                await repository.session.execute(text("SHOW transaction_isolation"))
            ).scalar_one()
            purge_entered.set()
        return await original_shared_execute(repository, envelope, mutation)

    monkeypatch.setattr(SharedWriteRepository, "execute", observed_shared_execute)
    commands = MemoryLifecycleCommands(pg_factory)
    original_redact = commands._redact_source_turns

    async def graph_root_redact(session, operation_id, content, preferred_turn_ids):
        assert preferred_turn_ids == {turn_id}
        return await original_redact(
            session, operation_id, content, preferred_turn_ids
        )

    monkeypatch.setattr(commands, "_redact_source_turns", graph_root_redact)
    delete_task = asyncio.create_task(commands.delete({
        "memory_id": str(owner_memory_id), "scope": "memory_and_sources"
    }))
    await asyncio.wait_for(purge_entered.wait(), timeout=3)
    assert purge_probe["isolation"] == "read committed"
    waiting = False
    for _ in range(100):
        async with pg_factory() as probe_session:
            wait_row = (await probe_session.execute(
                text(
                    "SELECT wait_event_type, wait_event FROM pg_stat_activity "
                    "WHERE pid = :pid"
                ),
                {"pid": purge_probe["pid"]},
            )).first()
        if wait_row is not None and wait_row[0] == "Lock":
            waiting = True
            break
        await asyncio.sleep(0.01)
    assert waiting, "purge never reached the advisory-lock wait"
    assert not delete_task.done()
    release.set()
    publish_result, delete_result = await asyncio.gather(publish_task, delete_task)
    assert delete_result["affected"]["memory_count"] == 2, delete_result
    assert publish_result.reconciled == 1
    assert delete_result["result"] == "deleted"
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        turn = await session.get(TurnRow, turn_id)
        active_count = (
            await session.execute(
                select(func.count()).select_from(DurableMemoryRow)
                .where(DurableMemoryRow.status == "active")
            )
        ).scalar_one()
        assert active_count == 0
        assert candidate.status == "invalidated"
        assert candidate.content is None
        assert turn.deleted_at is not None
    await _assert_no_live_tombstoned_dependencies(pg_factory)


class _BlockingDuplicateModel(_DynamicModel):
    def __init__(self, content: str, *, event_valid_from: datetime) -> None:
        super().__init__(content, event_valid_from=event_valid_from)
        self.first_reconcile = asyncio.Event()
        self.release = asyncio.Event()
        self.reconcile_calls = 0

    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        if "blind evidence-reading stage" in instructions:
            return await self.reading()
        if "Compare a foreground memory proposal" in instructions:
            return await self.comparison()
        if "Reconcile one independently verified claim" in instructions:
            self.reconcile_calls += 1
            payload = json.loads(messages[0]["content"])
            active = payload["applicable_active_memories"]
            assert active
            if self.reconcile_calls == 1:
                self.first_reconcile.set()
                await asyncio.wait_for(self.release.wait(), timeout=3)
                relation = "duplicate_of"
            else:
                relation = "historical_predecessor"
            return json.dumps({
                "relation": relation,
                "target_memory_id": active[0]["memory_id"],
                "replacement_content": self.content if relation != "duplicate_of" else None,
            })
        raise AssertionError("unexpected stage")


class _CurrentSupersedeModel(_DynamicModel):
    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        if "blind evidence-reading stage" in instructions:
            return await self.reading()
        if "Compare a foreground memory proposal" in instructions:
            return await self.comparison()
        if "Reconcile one independently verified claim" in instructions:
            payload = json.loads(messages[0]["content"])
            active = payload["applicable_active_memories"]
            assert active
            return json.dumps({
                "relation": "supersedes",
                "target_memory_id": active[0]["memory_id"],
                "replacement_content": self.content,
            })
        raise AssertionError("unexpected stage")


@pytest.mark.asyncio
async def test_duplicate_provenance_racing_supersede_reconciles_after_cas(pg_factory):
    old_time = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)
    new_time = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    old_content = "Jaco prefers compact reports."
    new_content = "Jaco prefers detailed reports."
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=2, content_revision=2)
        session.add(transcript)
        await session.flush()
        old_turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": old_content}], created_at=old_time,
        )
        new_turn = TurnRow(
            transcript_id=transcript.id, sequence=2, actor="owner",
            blocks=[{"type": "text", "text": new_content}], created_at=new_time,
        )
        session.add_all([old_turn, new_turn])
        target, _ = await DurableMemoryRepository(session).create_active(
            old_content, source_transcript_id=None, source_turn_id=None,
            record_kind="derived", memory_kind="preference",
            scope="cross_chat", scope_key="owner", durability="long_term",
            subject="Jaco", valid_from=old_time,
        )
        await session.commit()
        transcript_id = transcript.id
        old_turn_id, new_turn_id, target_id = old_turn.id, new_turn.id, target.id
    duplicate_candidate_id = await _enqueue(
        pg_factory, transcript_id=transcript_id, turn_id=old_turn_id,
        content=old_content, intake_path="foreground",
    )
    await _enqueue(
        pg_factory, transcript_id=transcript_id, turn_id=new_turn_id,
        content=new_content, intake_path="sweep",
    )
    duplicate_model = _BlockingDuplicateModel(
        old_content, event_valid_from=old_time
    )
    duplicate_service = MemoryReconciliationService(
        pg_factory, duplicate_model, batch_size=1
    )
    duplicate_task = asyncio.create_task(duplicate_service.run_once())
    await asyncio.wait_for(duplicate_model.first_reconcile.wait(), timeout=3)
    supersede_service = MemoryReconciliationService(
        pg_factory,
        _CurrentSupersedeModel(new_content, event_valid_from=new_time),
        batch_size=1,
    )
    supersede_result = await supersede_service.run_once()
    assert supersede_result.reconciled == 1
    duplicate_model.release.set()
    duplicate_result = await duplicate_task
    assert duplicate_result.reconciled == 1
    assert duplicate_result.blocked == 0
    assert duplicate_result.version_conflicts == 1
    assert duplicate_model.reconcile_calls == 2
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, duplicate_candidate_id)
        stale_target = await session.get(DurableMemoryRow, target_id)
        provenance = list((await session.execute(
            select(MemoryProvenanceRow).where(
                MemoryProvenanceRow.source_candidate_id == duplicate_candidate_id
            )
        )).scalars())
        records = list((await session.execute(
            select(MemoryReconciliationRecordRow)
            .where(MemoryReconciliationRecordRow.candidate_id == duplicate_candidate_id)
            .order_by(MemoryReconciliationRecordRow.created_at)
        )).scalars())
        assert candidate.status == "reconciled"
        assert stale_target.status == "superseded"
        assert records[-1].relation == "historical_predecessor"
        assert any(row.memory_id == target_id for row in provenance)
    await _assert_no_live_tombstoned_dependencies(pg_factory)


@pytest.mark.asyncio
async def test_sweep_recent_filler_cannot_move_temporal_horizon(pg_factory):
    old_time = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    filler_time = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    asserted_time = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
    content = "Jaco prefers compact reports."
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=2, content_revision=2)
        session.add(transcript)
        await session.flush()
        claim_turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": content}], created_at=old_time,
        )
        filler_turn = TurnRow(
            transcript_id=transcript.id, sequence=2, actor="owner",
            blocks=[{"type": "text", "text": "Yes, exactly."}], created_at=filler_time,
        )
        session.add_all([claim_turn, filler_turn])
        target, _ = await DurableMemoryRepository(session).create_active(
            "Jaco prefers detailed reports.", source_transcript_id=None, source_turn_id=None,
            record_kind="derived", memory_kind="preference", scope="cross_chat", scope_key="owner",
            durability="long_term", subject="Jaco",
            valid_from=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
        )
        await session.commit()
    result = await MemoryCandidateIntake(pg_factory).enqueue_many(
        [{
            "kind": "preference", "content": content, "scope": "cross_chat",
            "confidence": 0.9, "durability": "long_term", "proposed_action": "upsert",
            "subject": "Jaco",
            "evidence_refs": [
                {"turn_id": str(claim_turn.id), "span_ref": "text:0"},
                {"turn_id": str(filler_turn.id), "span_ref": "text:0"},
            ],
        }],
        source_transcript_id=transcript.id,
        source_turn_id=claim_turn.id,
        source_provider_evidence_id=None,
        proposer_model="sweep-test",
        intake_path="sweep",
        temporal_horizon_at=old_time,
    )
    assert result["accepted"] == 1
    run = await MemoryReconciliationService(
        pg_factory, _SupersedeModel(content, target.id, asserted_time)
    ).run_once()
    assert run.blocked == 1
    async with pg_factory() as session:
        conflict = (await session.execute(select(MemoryConflictRow))).scalar_one()
        record = (await session.execute(select(MemoryReconciliationRecordRow))).scalar_one()
        assert conflict.reason_code == "beyond_evidence_horizon"
        assert record.temporal_guard == "beyond_evidence_horizon"
        obligation = (await session.execute(select(MemoryObligationRow))).scalar_one()
        assert obligation.resolution_json["trigger_reason"] == "beyond_evidence_horizon"


@pytest.mark.asyncio
async def test_purge_holds_fence_first_and_publication_aborts(pg_factory, monkeypatch):
    turn_text = "Codename Raven. Jaco prefers concise reports."
    candidate_content = "Jaco prefers concise reports."
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": turn_text}],
        )
        session.add(turn)
        await session.flush()
        owner_memory, _ = await DurableMemoryRepository(session).create_active(
            "Codename Raven.", source_transcript_id=transcript.id,
            source_turn_id=turn.id, record_kind="owner_directed", memory_kind="fact",
            scope="cross_chat", scope_key="owner", durability="long_term",
        )
        await session.commit()
        transcript_id, turn_id, owner_memory_id = transcript.id, turn.id, owner_memory.id
    candidate_id = await _enqueue(
        pg_factory, transcript_id=transcript_id, turn_id=turn_id,
        content=candidate_content, intake_path="foreground",
    )
    commands = MemoryLifecycleCommands(pg_factory)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_redact = commands._redact_source_turns
    async def blocked_redact(session, operation_id, content, preferred_turn_ids):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=3)
        return await original_redact(session, operation_id, content, preferred_turn_ids)

    monkeypatch.setattr(commands, "_redact_source_turns", blocked_redact)
    delete_task = asyncio.create_task(commands.delete({
        "memory_id": str(owner_memory_id), "scope": "memory_and_sources"
    }))
    await asyncio.wait_for(entered.wait(), timeout=3)
    service = MemoryReconciliationService(pg_factory, _SimpleNewModel(candidate_content))
    publish_task = asyncio.create_task(service.run_once())
    await asyncio.sleep(0.05)
    assert not publish_task.done()
    release.set()
    delete_result = await delete_task
    assert delete_result["affected"]["candidate_count"] == 1, delete_result
    assert delete_result["result"] == "deleted"
    async with pg_factory() as session:
        candidate_after_purge = await session.get(MemoryCandidateRow, candidate_id)
        assert candidate_after_purge.status == "invalidated"
    publish_result = await publish_task
    assert publish_result.reconciled == 0
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        turn = await session.get(TurnRow, turn_id)
        active_count = (
            await session.execute(
                select(func.count()).select_from(DurableMemoryRow)
                .where(DurableMemoryRow.status == "active")
            )
        ).scalar_one()
        assert active_count == 0
        assert candidate.status == "invalidated"
        assert turn.deleted_at is not None
    await _assert_no_live_tombstoned_dependencies(pg_factory)


class _SerializationFailure(Exception):
    sqlstate = "40001"


@pytest.mark.asyncio
async def test_publication_serialization_failure_routes_to_reconciliation_retry(
    pg_factory, monkeypatch
):
    content = "Jaco prefers serialization failures to retry cleanly."
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": content}],
        )
        session.add(turn)
        await session.commit()
        transcript_id, turn_id = transcript.id, turn.id
    await _enqueue(
        pg_factory, transcript_id=transcript_id, turn_id=turn_id,
        content=content, intake_path="foreground",
    )
    service = MemoryReconciliationService(pg_factory, _SimpleNewModel(content))
    original_publish = service.publisher.publish
    calls = 0
    async def flaky_publish(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DBAPIError(None, None, _SerializationFailure("serialization"), False)
        return await original_publish(*args, **kwargs)

    monkeypatch.setattr(service.publisher, "publish", flaky_publish)
    result = await service.run_once()
    assert result.reconciled == 1
    assert result.version_conflicts == 1
    assert result.failures == 0
    assert calls == 2
    async with pg_factory() as session:
        memories = list((await session.execute(
            select(DurableMemoryRow).where(DurableMemoryRow.status == "active")
        )).scalars())
        assert len(memories) == 1


@pytest.mark.asyncio
async def test_explicit_remember_failure_budget_terminates_obligation(pg_factory, monkeypatch):
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": "Remember my rollback preference."}],
        )
        session.add(turn)
        await session.commit()
    commands = MemoryLifecycleCommands(pg_factory)
    async def fail_apply(*_args, **_kwargs):
        raise RuntimeError("forced persistent remember failure")

    monkeypatch.setattr(commands, "_apply_command", fail_apply)
    with pytest.raises(MemoryMutationError):
        await commands.remember({
            "content": "Jaco prefers strict rollback tests.",
            "kind": "preference", "scope": "cross_chat", "durability": "long_term",
        })
    async with pg_factory() as session:
        command = (await session.execute(select(MemoryCommandRow))).scalar_one()
        obligation = (await session.execute(select(MemoryObligationRow))).scalar_one()
        assert obligation.status == "pending"
        assert obligation.resolution_json["failure_count"] == 1
        command_id, obligation_id = command.id, obligation.id
    await commands._fail(command_id, RuntimeError("forced persistent remember failure"))
    async with pg_factory() as session:
        obligation = await session.get(MemoryObligationRow, obligation_id)
        assert obligation.status == "pending"
        assert obligation.resolution_json["failure_count"] == 2
    await commands._fail(command_id, RuntimeError("forced persistent remember failure"))
    async with pg_factory() as session:
        obligation = await session.get(MemoryObligationRow, obligation_id)
        assert obligation.status == "resolved"
        assert obligation.resolution_code == "failed_retry_exhausted"
        assert obligation.resolution_json["failure_count"] == 3
        assert obligation.resolved_at is not None
