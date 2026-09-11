"""Verifier model-contract hardening: the blind reading, comparison and reconciliation
stages fold spelling variants, drop unusable optional hints, and fail closed on
invalid required tokens."""

from __future__ import annotations

import json

import pytest
from atlas.memory.reconciliation import (
    MemoryReconciliationService,
    normalize_comparison_payload,
    normalize_reading_payload,
    normalize_relation_payload,
)
from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryIndependentReadingRow,
)
from sqlalchemy import select
from test_memory_state_machine import _seed


class _ContractModel:
    """Emits the kinds of values the first production verifier run actually returned."""

    model = "verifier-contract-test"

    def __init__(self, content: str, *, verdict: str = "Agree", relation: str = "New") -> None:
        self.content = content
        self.verdict = verdict
        self.relation = relation

    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        if "blind evidence-reading stage" in instructions:
            assert "never owner, personal, or global" in instructions
            return json.dumps({
                "extracted_claims": [self.content] + [f"Supporting detail {index}." for index in range(15)],
                "category": "preference",
                "scope": "owner",
                "durability": "episodic",
                "claim_principal": "personal",
                "event_valid_from": None,
                "event_valid_to": None,
            })
        if "Compare a foreground memory proposal" in instructions:
            assert "Any other verdict fails" in instructions
            return json.dumps({
                "verdict": self.verdict,
                "normalized_content": self.content,
                "category": "preference",
                "scope": "Cross-Chat",
                "durability": "Long Term",
            })
        if "Reconcile one independently verified claim" in instructions:
            return json.dumps({
                "relation": self.relation, "target_memory_id": None, "replacement_content": None,
            })
        raise AssertionError("unexpected model stage")


@pytest.mark.asyncio
async def test_reading_hints_outside_schema_are_dropped_not_fatal(pg_factory):
    content = "Jaco prefers local-first tools."
    candidate_id, _, _, _ = await _seed(pg_factory, content=content)
    service = MemoryReconciliationService(pg_factory, _ContractModel(content))
    result = await service.run_once()
    assert (result.reconciled, result.failures) == (1, 0), result
    assert result.contract_notes == {
        "blind_reading.claims_truncated": 8,
        "blind_reading.invalid_claim_principal": 1,
        "blind_reading.invalid_durability": 1,
        "blind_reading.invalid_scope": 1,
    }
    assert result.as_dict()["reconciliation_contract_notes"] == result.contract_notes
    async with pg_factory() as session:
        reading = (await session.execute(select(MemoryIndependentReadingRow))).scalar_one()
        assert len(reading.extracted_claims_json) == 8
        assert reading.extracted_claims_json[0] == content
        assert reading.scope is None
        assert reading.durability is None
        assert reading.claim_principal is None
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        assert candidate.status == "reconciled"
        memory = (await session.execute(select(DurableMemoryRow))).scalar_one()
        # Folded comparison hints still narrow the publication.
        assert memory.scope == "cross_chat"
        assert memory.durability == "long_term"
        assert memory.content == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "relation"),
    [("maybe", "New"), ("Agree", "merge_into")],
)
async def test_invalid_required_tokens_still_fail_closed(pg_factory, verdict, relation):
    content = "Jaco prefers local-first tools."
    candidate_id, _, _, _ = await _seed(pg_factory, content=content)
    result = await MemoryReconciliationService(
        pg_factory, _ContractModel(content, verdict=verdict, relation=relation), max_attempts=3
    ).run_once()
    assert (result.reconciled, result.failures) == (0, 1), result
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        assert candidate.status == "pending"  # requeued for another attempt
        assert (await session.execute(select(DurableMemoryRow))).scalar_one_or_none() is None


def test_verifier_normalization_is_syntax_only():
    data, notes = normalize_reading_payload({
        "extracted_claims": ["one", "", 3, "two"],
        "scope": "Global",
        "durability": "Long-Term",
        "claim_principal": " External ",
    })
    assert data["extracted_claims"] == ["one", "two"]
    assert (data["scope"], data["durability"], data["claim_principal"]) == (
        "cross_chat", "long_term", "external",
    )
    assert notes == {"claim_dropped": 2}

    data, notes = normalize_reading_payload({"scope": "permanent", "durability": "transient", "claim_principal": 7})
    assert (data["scope"], data["durability"], data["claim_principal"]) == (None, None, None)
    assert notes == {"invalid_claim_principal": 1, "invalid_durability": 1, "invalid_scope": 1}

    data, notes = normalize_comparison_payload({"verdict": "Narrow Scope", "scope": "session"})
    assert data["verdict"] == "narrow_scope"
    assert data["scope"] is None
    assert notes == {"invalid_scope": 1}
    # Required tokens are folded for spelling only; unknown values are left to fail.
    assert normalize_comparison_payload({"verdict": "maybe"})[0]["verdict"] == "maybe"
    assert normalize_relation_payload({"relation": "Duplicate-Of"})[0]["relation"] == "duplicate_of"
    assert normalize_relation_payload({"relation": "merge_into"})[0]["relation"] == "merge_into"
    assert normalize_reading_payload("not an object") == ("not an object", {})
