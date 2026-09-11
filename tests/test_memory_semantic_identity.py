from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from atlas.memory.candidates import MemoryCandidateIntake
from atlas.memory.reconciliation import MemoryReconciliationService
from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryProvenanceRow,
    MemoryReconciliationAttemptRow,
    TranscriptRow,
    TurnRow,
)
from sqlalchemy import func, select
from test_memory_state_machine import _create_verified_derived, _seed


class _IdentityModel:
    model = "semantic-identity-test"

    def __init__(
        self,
        content: str,
        *,
        category: str = "fact",
        durability: str = "long_term",
        relation: str = "new",
        target_memory_id: UUID | None = None,
        event_valid_from: datetime | None = None,
        replacement_content: str | None = None,
    ) -> None:
        self.content = content
        self.category = category
        self.durability = durability
        self.relation = relation
        self.target_memory_id = target_memory_id
        self.event_valid_from = event_valid_from
        self.replacement_content = replacement_content
        self.reconcile_instructions: str | None = None
        self.reconcile_payload: dict | None = None

    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str:
        if "blind evidence-reading stage" in instructions:
            return json.dumps({
                "extracted_claims": [self.content],
                "category": self.category,
                "scope": "cross_chat",
                "durability": self.durability,
                "claim_principal": "owner",
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
                "durability": self.durability,
            })
        if "Reconcile one independently verified claim" in instructions:
            self.reconcile_instructions = instructions
            self.reconcile_payload = json.loads(messages[0]["content"])
            return json.dumps({
                "relation": self.relation,
                "target_memory_id": (
                    str(self.target_memory_id) if self.target_memory_id else None
                ),
                "replacement_content": self.replacement_content,
            })
        raise AssertionError("unexpected model stage")


async def _set_memory_metadata(
    pg_factory,
    memory_id: UUID,
    *,
    durability: str | None = None,
    valid_from: datetime | None = None,
) -> None:
    async with pg_factory() as session:
        memory = await session.get(DurableMemoryRow, memory_id)
        assert memory is not None
        if durability is not None:
            memory.durability = durability
        if valid_from is not None:
            memory.valid_from = valid_from
        await session.commit()


async def _candidate_and_attempt(pg_factory, candidate_id: UUID):
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        attempt = (
            await session.execute(
                select(MemoryReconciliationAttemptRow)
                .where(MemoryReconciliationAttemptRow.candidate_id == candidate_id)
                .order_by(MemoryReconciliationAttemptRow.attempt_number.desc())
            )
        ).scalars().first()
        assert candidate is not None
        assert attempt is not None
        return candidate, attempt


@pytest.mark.asyncio
async def test_relation_instruction_defines_semantic_identity_and_has_metadata(pg_factory):
    content = (
        "As of September 2026, Jaco works through TGP in underground mining engineering "
        "at Cullinan Diamond Mine in South Africa."
    )
    valid_from = datetime(2026, 9, 1, tzinfo=UTC)
    target_id = await _create_verified_derived(
        pg_factory, content=content, memory_kind="project_state", valid_from=valid_from
    )
    await _seed(pg_factory, content=content, kind="fact", durability="long_term")
    model = _IdentityModel(
        content,
        category="fact",
        durability="long_term",
        relation="duplicate_of",
        target_memory_id=target_id,
        event_valid_from=valid_from,
    )
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.reconciled == 1
    instructions = model.reconcile_instructions or ""
    assert "Semantic identity is about the proposition" in instructions
    assert "even when their kind, scope, durability, subject, namespace, or wording" in instructions
    assert "Use narrows only when" in instructions
    assert "Use new only when" in instructions
    assert "Temporal qualifiers remain semantically significant" in instructions
    payload = model.reconcile_payload
    assert payload is not None
    active = next(
        item for item in payload["applicable_active_memories"]
        if item["memory_id"] == str(target_id)
    )
    assert active["content"] == content
    assert active["kind"] == "project_state"
    assert active["scope"] == "cross_chat"
    assert active["durability"] == "long_term"
    assert active["event_valid_from"].startswith("2026-09-01")


@pytest.mark.asyncio
async def test_same_proposition_different_kind_is_duplicate(pg_factory):
    content = "Jaco works in underground mining engineering through TGP."
    target_id = await _create_verified_derived(
        pg_factory, content=content, memory_kind="project_state"
    )
    candidate_id, _, turn_id, _ = await _seed(
        pg_factory, content=content, kind="fact", durability="long_term"
    )
    model = _IdentityModel(
        content,
        category="fact",
        durability="long_term",
        relation="duplicate_of",
        target_memory_id=target_id,
    )
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.reconciled == 1
    candidate, attempt = await _candidate_and_attempt(pg_factory, candidate_id)
    assert candidate.status == "reconciled"
    assert attempt.result_json["result"] == "lineage_added"
    async with pg_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(DurableMemoryRow))
        ).scalar_one() == 1
        provenance = (
            await session.execute(
                select(MemoryProvenanceRow).where(
                    MemoryProvenanceRow.memory_id == target_id,
                    MemoryProvenanceRow.source_turn_id == turn_id,
                )
            )
        ).scalar_one()
        assert provenance.relationship == "evidence_source"


@pytest.mark.asyncio
async def test_same_proposition_different_durability_is_duplicate(pg_factory):
    content = "Jaco works through TGP at Cullinan Diamond Mine."
    target_id = await _create_verified_derived(
        pg_factory, content=content, memory_kind="fact"
    )
    candidate_id, _, _, _ = await _seed(
        pg_factory, content=content, kind="fact", durability="short_term"
    )
    model = _IdentityModel(
        content,
        category="fact",
        durability="short_term",
        relation="duplicate_of",
        target_memory_id=target_id,
    )
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.reconciled == 1
    assert result.retained_short_term == 0
    candidate, attempt = await _candidate_and_attempt(pg_factory, candidate_id)
    assert candidate.status == "reconciled"
    assert candidate.review_after is None
    assert attempt.result_json["result"] == "lineage_added"
    async with pg_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(DurableMemoryRow))
        ).scalar_one() == 1
        target = await session.get(DurableMemoryRow, target_id)
        assert target is not None
        assert target.durability == "long_term"


@pytest.mark.asyncio
async def test_live_employment_paraphrase_is_duplicate(pg_factory):
    existing = (
        "As of September 2026, Jaco works through TGP in underground mining engineering "
        "at Cullinan Diamond Mine in South Africa."
    )
    paraphrase = (
        "As of September 2026, the owner works in underground mining engineering at "
        "Cullinan Diamond Mine in South Africa through TGP."
    )
    target_id = await _create_verified_derived(
        pg_factory, content=existing, memory_kind="fact"
    )
    candidate_id, _, turn_id, _ = await _seed(
        pg_factory,
        content=paraphrase,
        kind="project_state",
        durability="short_term",
    )
    model = _IdentityModel(
        paraphrase,
        category="project_state",
        durability="short_term",
        relation="duplicate_of",
        target_memory_id=target_id,
    )
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.reconciled == 1
    candidate, attempt = await _candidate_and_attempt(pg_factory, candidate_id)
    assert candidate.status == "reconciled"
    assert attempt.result_json["result"] == "lineage_added"
    async with pg_factory() as session:
        memories = list((await session.execute(select(DurableMemoryRow))).scalars())
        assert len(memories) == 1
        assert memories[0].id == target_id
        assert memories[0].content == existing
        lineage = (
            await session.execute(
                select(MemoryProvenanceRow).where(
                    MemoryProvenanceRow.memory_id == target_id,
                    MemoryProvenanceRow.source_turn_id == turn_id,
                    MemoryProvenanceRow.relationship == "evidence_source",
                )
            )
        ).scalar_one()
        assert lineage.source_turn_id == turn_id


@pytest.mark.asyncio
async def test_genuinely_narrower_claim_merges(pg_factory):
    existing = "Jaco works in underground mining engineering."
    narrower = (
        "As of September 2026, Jaco works through TGP in underground mining engineering "
        "at Cullinan Diamond Mine."
    )
    target_id = await _create_verified_derived(
        pg_factory, content=existing, memory_kind="fact"
    )
    candidate_id, _, _, _ = await _seed(
        pg_factory, content=narrower, kind="fact", durability="long_term"
    )
    model = _IdentityModel(
        narrower,
        category="fact",
        durability="long_term",
        relation="narrows",
        target_memory_id=target_id,
        replacement_content=narrower,
    )
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.reconciled == 1
    candidate, attempt = await _candidate_and_attempt(pg_factory, candidate_id)
    assert candidate.status == "reconciled"
    assert attempt.result_json["result"] == "created"
    async with pg_factory() as session:
        target = await session.get(DurableMemoryRow, target_id)
        assert target is not None
        assert target.status == "superseded"
        active = list(
            (
                await session.execute(
                    select(DurableMemoryRow).where(DurableMemoryRow.status == "active")
                )
            ).scalars()
        )
        assert len(active) == 1
        replacement = active[0]
        assert replacement.content == narrower
        merged = (
            await session.execute(
                select(MemoryProvenanceRow).where(
                    MemoryProvenanceRow.memory_id == replacement.id,
                    MemoryProvenanceRow.relationship == "merged_from",
                    MemoryProvenanceRow.source_memory_id == target_id,
                )
            )
        ).scalar_one()
        assert merged.source_memory_id == target_id


async def _create_owner_turn(pg_factory, texts: list[str]) -> tuple[UUID, UUID]:
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id,
            sequence=1,
            actor="owner",
            blocks=[{"type": "text", "text": text} for text in texts],
        )
        session.add(turn)
        await session.commit()
        return transcript.id, turn.id


async def _enqueue_for_turn(
    pg_factory,
    transcript_id: UUID,
    turn_id: UUID,
    *,
    content: str,
    span_ref: str,
) -> UUID:
    result = await MemoryCandidateIntake(pg_factory).enqueue_many(
        [{
            "kind": "fact",
            "content": content,
            "scope": "cross_chat",
            "confidence": 0.9,
            "durability": "long_term",
            "proposed_action": "upsert",
            "subject": "Jaco",
            "evidence_refs": [{"turn_id": str(turn_id), "span_ref": span_ref}],
        }],
        source_transcript_id=transcript_id,
        source_turn_id=turn_id,
        source_provider_evidence_id=None,
        proposer_model="semantic-identity-test",
        intake_path="foreground",
    )
    assert result["accepted"] == 1
    async with pg_factory() as session:
        candidate = (
            await session.execute(
                select(MemoryCandidateRow).where(
                    MemoryCandidateRow.source_turn_id == turn_id,
                    MemoryCandidateRow.content == content,
                )
            )
        ).scalar_one()
        return candidate.id


@pytest.mark.asyncio
async def test_distinct_claims_from_same_owner_turn_stay_distinct(pg_factory):
    first = "Jaco works in underground mining engineering through TGP."
    second = "Jaco is actively developing skills in Python and Docker."
    transcript_id, turn_id = await _create_owner_turn(pg_factory, [first, second])
    first_id = await _enqueue_for_turn(
        pg_factory, transcript_id, turn_id, content=first, span_ref="text:0"
    )
    first_model = _IdentityModel(first, relation="new")
    first_run = await MemoryReconciliationService(pg_factory, first_model).run_once()
    assert first_run.reconciled == 1
    second_id = await _enqueue_for_turn(
        pg_factory, transcript_id, turn_id, content=second, span_ref="text:1"
    )
    second_model = _IdentityModel(second, relation="new")
    second_run = await MemoryReconciliationService(pg_factory, second_model).run_once()
    assert second_run.reconciled == 1
    first_candidate, _ = await _candidate_and_attempt(pg_factory, first_id)
    second_candidate, _ = await _candidate_and_attempt(pg_factory, second_id)
    assert first_candidate.status == "reconciled"
    assert second_candidate.status == "reconciled"
    async with pg_factory() as session:
        active = list(
            (
                await session.execute(
                    select(DurableMemoryRow).where(DurableMemoryRow.status == "active")
                )
            ).scalars()
        )
        assert {row.content for row in active} == {first, second}


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["supersedes", "duplicate_of"])
async def test_temporal_difference_remains_model_judged(pg_factory, relation):
    august = datetime(2026, 8, 1, tzinfo=UTC)
    september = datetime(2026, 9, 1, tzinfo=UTC)
    existing = "In August 2026, Jaco worked in an earlier engineering state."
    later = "From September 2026, Jaco works in a later engineering state."
    target_id = await _create_verified_derived(
        pg_factory, content=existing, memory_kind="fact", valid_from=august
    )
    candidate_id, _, turn_id, _ = await _seed(
        pg_factory,
        content=later,
        kind="fact",
        durability="long_term",
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
    )
    model = _IdentityModel(
        later,
        category="fact",
        durability="long_term",
        relation=relation,
        target_memory_id=target_id,
        event_valid_from=september,
        replacement_content=(later if relation == "supersedes" else None),
    )
    result = await MemoryReconciliationService(pg_factory, model).run_once()
    assert result.reconciled == 1
    candidate, attempt = await _candidate_and_attempt(pg_factory, candidate_id)
    assert candidate.status == "reconciled"
    async with pg_factory() as session:
        target = await session.get(DurableMemoryRow, target_id)
        assert target is not None
        if relation == "supersedes":
            assert attempt.result_json["result"] == "created"
            assert target.status == "superseded"
            assert target.valid_to == september
            replacement = (
                await session.execute(
                    select(DurableMemoryRow).where(DurableMemoryRow.status == "active")
                )
            ).scalar_one()
            assert replacement.content == later
            assert replacement.valid_from == september
        else:
            assert attempt.result_json["result"] == "lineage_added"
            assert target.status == "active"
            assert target.valid_from == august
            assert (
                await session.execute(select(func.count()).select_from(DurableMemoryRow))
            ).scalar_one() == 1
            lineage = (
                await session.execute(
                    select(MemoryProvenanceRow).where(
                        MemoryProvenanceRow.memory_id == target_id,
                        MemoryProvenanceRow.source_turn_id == turn_id,
                        MemoryProvenanceRow.relationship == "evidence_source",
                    )
                )
            ).scalar_one()
            assert lineage.source_turn_id == turn_id
