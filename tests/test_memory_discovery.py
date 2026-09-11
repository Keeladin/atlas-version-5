"""Background discovery sweep: cursor, window, horizon, purge rewind and rotation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from atlas.memory.discovery import MemoryBackgroundDiscoveryService
from atlas.memory.durable import DurableMemoryRepository
from atlas.memory.lifecycle import MemoryLifecycleCommands
from atlas.memory.service import MemoryService
from atlas.persistence.models import (
    MemoryCandidateEvidenceRow,
    MemoryCandidateRow,
    MemoryDiscoveryStateRow,
    TranscriptRow,
    TurnRow,
)
from sqlalchemy import select

_BASE = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


async def _seed_transcript(
    pg_factory,
    texts: list[str | tuple[str, str]],
    *,
    kind: str = "owner",
    spacing_seconds: int = 60,
    base: datetime = _BASE,
    created_at_overrides: dict[int, datetime] | None = None,
    closed: bool = False,
) -> tuple[UUID, list[UUID]]:
    async with pg_factory() as session:
        transcript = TranscriptRow(
            kind=kind,
            next_turn_sequence=len(texts) + 1,
            content_revision=len(texts),
            updated_at=base,
            closed_at=(base if closed else None),
            **({"retention_policy": "dependency_protected"} if kind == "memory_review" else {}),
        )
        session.add(transcript)
        await session.flush()
        turn_ids: list[UUID] = []
        for sequence, entry in enumerate(texts, start=1):
            actor, text = entry if isinstance(entry, tuple) else ("owner", entry)
            created_at = (created_at_overrides or {}).get(
                sequence, base + timedelta(seconds=spacing_seconds * sequence)
            )
            turn = TurnRow(
                transcript_id=transcript.id, sequence=sequence, actor=actor,
                blocks=[{"type": "text", "text": text}], created_at=created_at,
            )
            session.add(turn)
            await session.flush()
            turn_ids.append(turn.id)
        await session.commit()
        return transcript.id, turn_ids


async def _append_turn(pg_factory, transcript_id: UUID, text: str, *, created_at=None) -> UUID:
    async with pg_factory() as session:
        transcript = await session.get(TranscriptRow, transcript_id)
        sequence = int(transcript.next_turn_sequence or 1)
        transcript.next_turn_sequence = sequence + 1
        transcript.content_revision = int(transcript.content_revision or 0) + 1
        turn = TurnRow(
            transcript_id=transcript_id, sequence=sequence, actor="owner",
            blocks=[{"type": "text", "text": text}],
            created_at=created_at or (_BASE + timedelta(seconds=60 * sequence)),
        )
        session.add(turn)
        await session.commit()
        return turn.id


async def _soft_delete(pg_factory, turn_id: UUID) -> None:
    async with pg_factory() as session:
        turn = await session.get(TurnRow, turn_id)
        turn.deleted_at = datetime.now(UTC)
        turn.blocks = [{"type": "text", "text": "[Content deleted by owner]"}]
        await session.commit()


class _HandleEchoModel:
    """Discovery stub: proposes one candidate per owner item, citing the supplied handle."""

    model = "discovery-test"

    def __init__(self, *, cite=None, fail_times: int = 0, during_call=None) -> None:
        self.cite = cite
        self.fail_times = fail_times
        self.during_call = during_call
        self.calls = 0
        self.payloads: list[list[dict]] = []

    def sequences(self, call: int = -1) -> list[int]:
        return [int(turn["sequence"]) for turn in self.payloads[call]]

    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        assert "background memory discovery sweep" in instructions
        self.calls += 1
        turns = json.loads(messages[0]["content"])["canonical_turns"]
        self.payloads.append(turns)
        if self.calls <= self.fail_times:
            raise RuntimeError("forced discovery failure")
        if self.during_call is not None:
            await self.during_call()
        items = [
            item for turn in turns if turn["actor"] == "owner" for item in turn["items"]
        ]
        handles = self.cite(turns) if self.cite is not None else [item["handle"] for item in items]
        texts = {item["handle"]: item["text"] for item in items}
        candidates = [
            {
                "kind": "preference",
                "content": texts.get(handle, f"Cited {handle}"),
                "scope": "cross_chat",
                "confidence": 0.8,
                "durability": "long_term",
                "proposed_action": "upsert",
                "subject": "Jaco",
                "evidence_refs": [{"handle": handle}],
            }
            for handle in handles[:8]
        ]
        return json.dumps({"memory_candidates": candidates})


def _service(pg_factory, model, **overrides) -> MemoryBackgroundDiscoveryService:
    return MemoryBackgroundDiscoveryService(pg_factory, model, **overrides)


async def _cursor(pg_factory, transcript_id: UUID) -> int | None:
    async with pg_factory() as session:
        state = await session.get(MemoryDiscoveryStateRow, transcript_id)
        return int(state.last_scanned_sequence) if state is not None else None


async def _candidates(pg_factory) -> list[MemoryCandidateRow]:
    async with pg_factory() as session:
        return list((await session.execute(
            select(MemoryCandidateRow).order_by(MemoryCandidateRow.created_at, MemoryCandidateRow.id)
        )).scalars())


def _texts(count: int, prefix: str = "Jaco prefers option") -> list[str]:
    return [f"{prefix} number {index}." for index in range(1, count + 1)]


@pytest.mark.asyncio
async def test_sweep_accepts_candidates_citing_supplied_handles(pg_factory):
    transcript_id, turn_ids = await _seed_transcript(pg_factory, _texts(3))
    model = _HandleEchoModel()
    result = await _service(pg_factory, model).run_once()
    assert (result.transcripts_seen, result.turns_scanned) == (1, 3)
    assert (result.accepted, result.duplicates, result.rejected, result.failures) == (3, 0, 0, 0)
    assert model.sequences() == [1, 2, 3]
    assert [item["handle"] for turn in model.payloads[0] for item in turn["items"]] == [
        "o1.0", "o2.0", "o3.0",
    ]

    candidates = await _candidates(pg_factory)
    assert len(candidates) == 3
    async with pg_factory() as session:
        anchor = await session.get(TurnRow, turn_ids[-1])
        for candidate in candidates:
            assert candidate.intake_path == "sweep"
            assert candidate.origin == "conversation"
            assert candidate.proposer_model == "discovery-test"
            assert candidate.source_transcript_id == transcript_id
            assert candidate.source_turn_id == anchor.id
            assert candidate.temporal_horizon_at == anchor.created_at
        evidence = list((await session.execute(select(MemoryCandidateEvidenceRow))).scalars())
        assert {(row.turn_id, row.span_ref) for row in evidence} == {
            (turn_id, "text:0") for turn_id in turn_ids
        }
    assert await _cursor(pg_factory, transcript_id) == 3


@pytest.mark.asyncio
async def test_sweep_windows_are_disjoint_across_runs_and_batch_thirteen_turns(pg_factory):
    transcript_id, _ = await _seed_transcript(pg_factory, _texts(13))
    # Cite only unscanned turns so the eight-candidate cap does not mask batching.
    model = _HandleEchoModel(cite=lambda turns: [
        item["handle"] for turn in turns if turn["sequence"] > 8 for item in turn["items"]
    ] or [item["handle"] for turn in turns for item in turn["items"]])
    service = _service(pg_factory, model, max_turns_per_transcript=8, context_turns=4)

    first = await service.run_once()
    assert (first.turns_scanned, first.accepted) == (8, 8)
    assert model.sequences(0) == list(range(1, 9))
    assert await _cursor(pg_factory, transcript_id) == 8

    second = await service.run_once()
    assert (second.turns_scanned, second.accepted, second.duplicates) == (5, 5, 0)
    # Four context turns precede the scanned turns; they are visible, not scanned.
    assert model.sequences(1) == list(range(5, 14))
    assert await _cursor(pg_factory, transcript_id) == 13
    candidates = await _candidates(pg_factory)
    assert len(candidates) == 13

    third = await service.run_once()
    assert third.transcripts_seen == 0
    assert model.calls == 2


@pytest.mark.asyncio
async def test_context_turns_are_visible_evidence_but_never_scanned_or_anchoring(pg_factory):
    transcript_id, turn_ids = await _seed_transcript(pg_factory, _texts(13))
    service = _service(pg_factory, _HandleEchoModel(), max_turns_per_transcript=8)
    assert (await service.run_once()).accepted == 8

    # Second window: context o5..o8, scan o9..o13. Cite a context turn and the last turn.
    model = _HandleEchoModel(cite=lambda turns: ["o6.0", "o13.0"])
    result = _service(pg_factory, model, max_turns_per_transcript=8, context_turns=4)
    outcome = await result.run_once()
    assert (outcome.turns_scanned, outcome.accepted, outcome.duplicates, outcome.rejected) == (
        5, 1, 1, 0,
    )
    async with pg_factory() as session:
        anchor = await session.get(TurnRow, turn_ids[12])
        newest = (await session.execute(
            select(MemoryCandidateRow).order_by(MemoryCandidateRow.created_at.desc()).limit(1)
        )).scalar_one()
        assert newest.source_turn_id == anchor.id
        assert newest.temporal_horizon_at == anchor.created_at
        evidence = list((await session.execute(
            select(MemoryCandidateEvidenceRow).where(
                MemoryCandidateEvidenceRow.candidate_id == newest.id
            )
        )).scalars())
        assert {row.turn_id for row in evidence} == {turn_ids[12]}
    assert await _cursor(pg_factory, transcript_id) == 13


@pytest.mark.asyncio
async def test_sweep_horizon_is_max_created_at_of_scan_window(pg_factory):
    late = _BASE + timedelta(days=2)
    _, turn_ids = await _seed_transcript(
        pg_factory, _texts(3), created_at_overrides={2: late},
    )
    result = await _service(pg_factory, _HandleEchoModel()).run_once()
    assert result.accepted == 3
    for candidate in await _candidates(pg_factory):
        assert candidate.temporal_horizon_at == late
        assert candidate.source_turn_id == turn_ids[-1]


@pytest.mark.asyncio
async def test_swept_turns_are_not_reproposed_while_earlier_candidate_is_live(pg_factory):
    transcript_id, _ = await _seed_transcript(pg_factory, _texts(3))
    assert (await _service(pg_factory, _HandleEchoModel()).run_once()).accepted == 3
    await _append_turn(pg_factory, transcript_id, "Jaco prefers option number 4.")

    model = _HandleEchoModel(cite=lambda turns: ["o1.0", "o4.0"])
    result = await _service(pg_factory, model).run_once()
    assert model.sequences() == [1, 2, 3, 4]
    assert (result.turns_scanned, result.accepted, result.duplicates, result.rejected) == (
        1, 1, 1, 0,
    )
    assert len(await _candidates(pg_factory)) == 4
    assert await _cursor(pg_factory, transcript_id) == 4


@pytest.mark.asyncio
async def test_sweep_failure_leaves_cursor_unadvanced_and_retries(pg_factory):
    transcript_id, _ = await _seed_transcript(pg_factory, _texts(3))
    model = _HandleEchoModel(fail_times=1)
    service = _service(pg_factory, model)

    first = await service.run_once()
    assert (first.failures, first.accepted, first.stale) == (1, 0, 0)
    assert await _candidates(pg_factory) == []
    # The attempt is recorded (rotation) without moving the cursor.
    assert await _cursor(pg_factory, transcript_id) == 0
    async with pg_factory() as session:
        state = await session.get(MemoryDiscoveryStateRow, transcript_id)
        assert state.updated_at is not None

    second = await service.run_once()
    assert (second.failures, second.accepted) == (0, 3)
    assert model.sequences() == [1, 2, 3]
    assert await _cursor(pg_factory, transcript_id) == 3


@pytest.mark.asyncio
async def test_sweep_excludes_soft_deleted_turns(pg_factory):
    transcript_id, turn_ids = await _seed_transcript(pg_factory, _texts(3))
    await _soft_delete(pg_factory, turn_ids[1])
    model = _HandleEchoModel(cite=lambda turns: ["o1.0", "o2.0", "o3.0"])
    result = await _service(pg_factory, model).run_once()
    assert model.sequences() == [1, 3]
    assert (result.turns_scanned, result.accepted, result.rejected) == (2, 2, 1)
    assert await _cursor(pg_factory, transcript_id) == 3


@pytest.mark.asyncio
async def test_purge_rewinds_discovery_cursor_and_rescan_skips_redacted_turns(pg_factory):
    transcript_id, turn_ids = await _seed_transcript(pg_factory, _texts(3))
    assert (await _service(pg_factory, _HandleEchoModel()).run_once()).accepted == 3
    assert await _cursor(pg_factory, transcript_id) == 3

    await _soft_delete(pg_factory, turn_ids[1])
    async with pg_factory() as session:
        await MemoryLifecycleCommands(pg_factory)._invalidate_transcript_derivatives(
            session, {turn_ids[1]}
        )
        await session.commit()
    assert await _cursor(pg_factory, transcript_id) == 1
    async with pg_factory() as session:
        transcript = await session.get(TranscriptRow, transcript_id)
        assert int(transcript.content_revision) == 4

    model = _HandleEchoModel()
    result = await _service(pg_factory, model).run_once()
    assert model.sequences() == [1, 3]
    # Both surviving turns already carry live candidates: duplicates, never copies.
    assert (result.turns_scanned, result.accepted, result.duplicates, result.rejected) == (
        1, 0, 2, 0,
    )
    assert await _cursor(pg_factory, transcript_id) == 3


@pytest.mark.asyncio
async def test_owner_delete_rewinds_discovery_cursor_end_to_end(pg_factory):
    secret = "The temporary code is ORBIT-7."
    transcript_id, turn_ids = await _seed_transcript(
        pg_factory, ["Calibration alpha remains useful.", f"Remember this: {secret}", "Thanks."],
    )
    assert (await _service(pg_factory, _HandleEchoModel()).run_once()).accepted == 3
    async with pg_factory() as session:
        memory, _ = await DurableMemoryRepository(session).create_active(
            secret,
            source_transcript_id=transcript_id,
            source_turn_id=turn_ids[1],
            record_kind="derived",
            grounding_status="verified",
            owner_assertion_turn_id=turn_ids[1],
            memory_kind="fact",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
        )
        await session.commit()
        memory_id = memory.id

    deleted = await MemoryService(pg_factory).delete({"memory_id": str(memory_id)})
    assert deleted["result"] == "deleted"
    assert await _cursor(pg_factory, transcript_id) == 1
    async with pg_factory() as session:
        redacted = await session.get(TurnRow, turn_ids[1])
        assert redacted.deleted_at is not None
        assert secret not in str(redacted.blocks)

    model = _HandleEchoModel()
    result = await _service(pg_factory, model).run_once()
    assert model.sequences() == [1, 3]
    assert secret not in json.dumps(model.payloads[-1])
    assert result.failures == 0
    assert await _cursor(pg_factory, transcript_id) == 3


@pytest.mark.asyncio
async def test_fully_swept_transcript_and_deleted_tail_are_not_eligible(pg_factory):
    transcript_id, _ = await _seed_transcript(pg_factory, _texts(3))
    service = _service(pg_factory, _HandleEchoModel())
    assert (await service.run_once()).accepted == 3
    assert await service._eligible_transcripts() == []

    tail = await _append_turn(pg_factory, transcript_id, "Jaco prefers option number 4.")
    assert await service._eligible_transcripts() == [transcript_id]
    await _soft_delete(pg_factory, tail)
    assert await service._eligible_transcripts() == []
    result = await service.run_once()
    assert (result.transcripts_seen, result.turns_scanned) == (0, 0)


@pytest.mark.asyncio
async def test_sweep_respects_max_transcripts_and_rotates_never_swept_first(pg_factory):
    oldest, _ = await _seed_transcript(
        pg_factory, _texts(2, "Oldest chat fact"), base=_BASE, closed=True,
    )
    middle, _ = await _seed_transcript(
        pg_factory, _texts(2, "Middle chat fact"), base=_BASE + timedelta(hours=1), closed=True,
    )
    newest, _ = await _seed_transcript(
        pg_factory, _texts(2, "Newest chat fact"), base=_BASE + timedelta(hours=2),
    )
    failing_oldest = _HandleEchoModel(fail_times=1)
    service = _service(pg_factory, failing_oldest, max_transcripts=2)
    assert await service._eligible_transcripts() == [oldest, middle]

    first = await service.run_once()
    assert (first.transcripts_seen, first.failures, first.accepted) == (2, 1, 2)
    # The failed oldest transcript was attempted and rotates behind the never-swept newest.
    assert await service._eligible_transcripts() == [newest, oldest]

    second = await service.run_once()
    assert (second.transcripts_seen, second.failures, second.accepted) == (2, 0, 4)
    assert await service._eligible_transcripts() == []


@pytest.mark.asyncio
async def test_sweep_skips_advance_when_window_turn_is_purged_during_discovery(pg_factory):
    transcript_id, turn_ids = await _seed_transcript(pg_factory, _texts(3))

    async def purge_turn_two():
        await _soft_delete(pg_factory, turn_ids[1])
        async with pg_factory() as session:
            await MemoryLifecycleCommands(pg_factory)._invalidate_transcript_derivatives(
                session, {turn_ids[1]}
            )
            await session.commit()

    model = _HandleEchoModel(during_call=purge_turn_two)
    result = await _service(pg_factory, model).run_once()
    assert result.stale == 1
    assert result.failures == 0
    assert await _cursor(pg_factory, transcript_id) == 0
    # Candidates citing the purged turn were invalidated by the purge itself.
    statuses = {
        candidate.status for candidate in await _candidates(pg_factory)
        if candidate.source_turn_id == turn_ids[2]
    }
    assert statuses <= {"pending", "invalidated"}

    again = _HandleEchoModel()
    second = await _service(pg_factory, again).run_once()
    assert again.sequences() == [1, 3]
    assert second.stale == 0
    assert await _cursor(pg_factory, transcript_id) == 3


@pytest.mark.asyncio
async def test_sweep_advances_when_turns_are_appended_during_discovery(pg_factory):
    transcript_id, _ = await _seed_transcript(pg_factory, _texts(3))

    async def append_turn_four():
        await _append_turn(pg_factory, transcript_id, "Jaco prefers option number 4.")

    model = _HandleEchoModel(during_call=append_turn_four)
    result = await _service(pg_factory, model).run_once()
    assert (result.accepted, result.stale) == (3, 0)
    assert await _cursor(pg_factory, transcript_id) == 3

    again = _HandleEchoModel()
    second = await _service(pg_factory, again).run_once()
    assert again.sequences() == [1, 2, 3, 4]
    assert (second.turns_scanned, second.accepted) == (1, 1)
    assert await _cursor(pg_factory, transcript_id) == 4


@pytest.mark.asyncio
async def test_sweep_ignores_non_owner_transcripts(pg_factory):
    await _seed_transcript(pg_factory, ["An owner assertion inside a review."], kind="memory_review")
    model = _HandleEchoModel()
    result = await _service(pg_factory, model).run_once()
    assert result.transcripts_seen == 0
    assert model.calls == 0
    assert await _candidates(pg_factory) == []


@pytest.mark.asyncio
async def test_run_memory_index_once_reports_discovery_statuses(pg_factory, tmp_path, monkeypatch):
    from atlas.config import Settings
    from atlas.memory import maintenance

    await _seed_transcript(pg_factory, _texts(2))
    monkeypatch.setattr(maintenance, "get_session_factory", lambda: pg_factory)
    monkeypatch.setattr(maintenance, "_embedder", lambda settings: None)

    disabled = await maintenance.run_memory_index_once(
        Settings(openai_api_key_file=None, memory_reconciliation_enabled=False)
    )
    assert disabled["discovery_status"] == "disabled"
    unavailable = await maintenance.run_memory_index_once(
        Settings(openai_api_key_file=None, memory_reconciliation_enabled=True)
    )
    assert unavailable["discovery_status"] == "unavailable"

    key_file = tmp_path / "openai-key"
    key_file.write_text("sk-test\n")
    echo = _HandleEchoModel()

    class _MaintenanceModel:
        model = "maintenance-test"

        async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
            if "background memory discovery sweep" in instructions:
                return await echo.complete_text(instructions=instructions, messages=messages)
            raise RuntimeError("stage not scripted")

    monkeypatch.setattr(maintenance, "_model", lambda settings, *, model: _MaintenanceModel())
    ready = await maintenance.run_memory_index_once(
        Settings(openai_api_key_file=key_file, memory_reconciliation_enabled=True)
    )
    assert ready["discovery_status"] == "ready"
    assert ready["discovery_transcripts_seen"] == 1
    assert ready["discovery_turns_scanned"] == 2
    assert ready["discovery_accepted"] == 2
    assert ready["discovery_stale"] == 0
    assert ready["discovery_failures"] == 0

    await _seed_transcript(
        pg_factory, _texts(1, "Second chat fact"), base=_BASE + timedelta(hours=1), closed=True,
    )
    echo.fail_times = echo.calls + 1
    degraded = await maintenance.run_memory_index_once(
        Settings(openai_api_key_file=key_file, memory_reconciliation_enabled=True)
    )
    assert degraded["discovery_status"] == "degraded"
    assert degraded["discovery_failures"] == 1


class _LooseVocabularyModel(_HandleEchoModel):
    """Returns the kinds of values the first production sweep actually emitted."""

    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        assert "never global" in instructions and "never a word" in instructions
        turns = json.loads(messages[0]["content"])["canonical_turns"]
        self.calls += 1
        self.payloads.append(turns)
        items = [item for turn in turns if turn["actor"] == "owner" for item in turn["items"]]
        handles = [item["handle"] for item in items]
        base = {"proposed_action": "upsert", "subject": "Jaco", "durability": "long_term"}
        return json.dumps({"memory_candidates": [
            {**base, "kind": "preference", "content": "Valid strict candidate.", "scope": "cross_chat",
             "confidence": 0.8, "evidence_refs": [{"handle": handles[0]}]},
            {**base, "kind": "Project State", "content": "Spelling-variant candidate.", "scope": "global",
             "confidence": 0.9, "durability": "long-term", "evidence_refs": [{"handle": handles[1]}]},
            {**base, "kind": "location", "content": "Unknown kind candidate.", "scope": "cross_chat",
             "confidence": 0.7, "evidence_refs": [{"handle": handles[2]}]},
            {**base, "kind": "career_goal", "content": "Another unknown kind.", "scope": "chat",
             "confidence": "high", "evidence_refs": [{"handle": handles[0]}]},
            {**base, "kind": "fact", "content": "Word confidence candidate.", "scope": "cross_chat",
             "confidence": "medium", "evidence_refs": [{"handle": handles[1]}]},
            {**base, "kind": "fact", "content": "Semantic alias candidate.", "scope": "cross_chat",
             "confidence": 0.5, "durability": "permanent", "evidence_refs": [{"handle": handles[2]}]},
            "not an object at all",
        ]})


@pytest.mark.asyncio
async def test_out_of_schema_proposals_reject_themselves_not_the_batch(pg_factory, caplog):
    transcript_id, _ = await _seed_transcript(pg_factory, _texts(3))
    model = _LooseVocabularyModel()
    result = await _service(pg_factory, model).run_once()
    assert (result.accepted, result.rejected, result.failures, result.duplicates) == (2, 5, 0, 0)
    assert result.rejections == {
        "invalid_candidate": 1,
        "invalid_confidence": 1,
        "invalid_durability": 1,
        "invalid_kind": 2,
    }
    assert "rejected 5 of 7 proposals" in caplog.text
    assert "candidate." not in caplog.text  # never proposal text

    candidates = {candidate.content: candidate for candidate in await _candidates(pg_factory)}
    assert set(candidates) == {"Valid strict candidate.", "Spelling-variant candidate."}
    folded = candidates["Spelling-variant candidate."]
    assert (folded.kind, folded.scope, folded.durability) == ("project_state", "cross_chat", "long_term")
    assert await _cursor(pg_factory, transcript_id) == 3


def test_candidate_normalization_is_syntax_only():
    from atlas.memory.candidates import MemoryCandidate, normalize_candidate_payload

    base = {"content": "x", "proposed_action": "upsert", "evidence_refs": [{"handle": "o1.0"}]}
    folded = MemoryCandidate.model_validate({
        **base, "kind": " Project  State ", "scope": "Global", "confidence": 0.75,
        "durability": "Short-Term",
    })
    assert (folded.kind, folded.scope, folded.confidence, folded.durability) == (
        "project_state", "cross_chat", 0.75, "short_term",
    )
    assert normalize_candidate_payload({"scope": "cross-chat"})["scope"] == "cross_chat"
    assert normalize_candidate_payload("not a dict") == "not a dict"
    # Meaning is never reinterpreted: words, synonyms and unknown kinds stay invalid.
    for bad in (
        {"kind": "location", "scope": "cross_chat", "confidence": 0.5, "durability": "long_term"},
        {"kind": "fact", "scope": "owner", "confidence": 0.5, "durability": "long_term"},
        {"kind": "fact", "scope": "all", "confidence": 0.5, "durability": "long_term"},
        {"kind": "fact", "scope": "chat", "confidence": "high", "durability": "long_term"},
        {"kind": "fact", "scope": "chat", "confidence": 0.5, "durability": "permanent"},
        {"kind": "fact", "scope": "chat", "confidence": 0.5, "durability": "transient"},
    ):
        with pytest.raises(ValueError):
            MemoryCandidate.model_validate({**base, **bad})
