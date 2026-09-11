"""Explicit remember/correct commands ride the candidate pipeline.

Behavioral coverage for the remember-obligation state machine named in
``test_memory_transition_matrix``: intake, publication success, terminal
publication failure, automatic retry, owner retry, retry budget exhaustion,
and evidence purge.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from atlas.memory.durable import DurableMemoryRepository, memory_fingerprint
from atlas.memory.explicit import OWNER_RETRY_BUDGET, settle_explicit_obligation
from atlas.memory.lifecycle import MemoryLifecycleCommands
from atlas.memory.reconciliation import (
    MemoryCandidateLeaseRepository,
    MemoryReconciliationService,
)
from atlas.memory.service import MemoryService
from atlas.memory.state_machine import PublicationOutcome, PublicationTerminality
from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryCommandRow,
    MemoryObligationRow,
    MemoryProvenanceRow,
    TranscriptRow,
    TurnRow,
)
from atlas.runtime.conversation import render_memory_outcomes
from sqlalchemy import func, select


async def _seed_owner_turn(pg_factory, text: str) -> tuple[UUID, UUID]:
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": text}],
        )
        session.add(turn)
        await session.commit()
        return transcript.id, turn.id


class _OwnerClaimModel:
    """Verifier stub that reads the leased candidate and treats it as an owner claim."""

    model = "explicit-verifier-test"

    def __init__(
        self,
        factory,
        *,
        claim_principal: str = "owner",
        scope: str | None = None,
        fail_stage: str | None = None,
        reconcile=None,
        event_time=None,
    ) -> None:
        self.factory = factory
        self.claim_principal = claim_principal
        self.scope = scope
        self.fail_stage = fail_stage
        # Optional overrides: a dict or callable(candidate) -> dict for the
        # reconcile stage, and a datetime or callable(observed_at) for the
        # claim's event time.
        self.reconcile = reconcile
        self.event_time = event_time
        self.calls: dict[str, int] = {"blind": 0, "compare": 0, "reconcile": 0}

    async def _leased(self) -> tuple[MemoryCandidateRow, datetime | None]:
        async with self.factory() as session:
            candidate = (
                await session.execute(
                    select(MemoryCandidateRow).where(MemoryCandidateRow.status == "leased")
                )
            ).scalars().one()
            turn = await session.get(TurnRow, candidate.source_turn_id)
            return candidate, (turn.created_at if turn is not None else None)

    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        candidate, observed_at = await self._leased()
        scope = self.scope or candidate.scope
        if "blind evidence-reading stage" in instructions:
            self.calls["blind"] += 1
            if self.fail_stage == "blind":
                raise RuntimeError("forced verifier failure")
            # The owner statement establishes the current state at observation
            # time, so the claim's event time is the evidence turn's timestamp.
            event_time = (
                self.event_time(observed_at) if callable(self.event_time)
                else (self.event_time or observed_at)
            )
            return json.dumps({
                "extracted_claims": [candidate.content],
                "category": candidate.kind,
                "scope": scope,
                "durability": candidate.durability,
                "claim_principal": self.claim_principal,
                "event_valid_from": event_time.isoformat() if event_time else None,
                "event_valid_to": None,
            })
        if "Compare a foreground memory proposal" in instructions:
            self.calls["compare"] += 1
            return json.dumps({
                "verdict": "agree",
                "normalized_content": candidate.content,
                "category": candidate.kind,
                "scope": scope,
                "durability": candidate.durability,
            })
        if "Reconcile one independently verified claim" in instructions:
            self.calls["reconcile"] += 1
            if self.reconcile is not None:
                payload = (
                    self.reconcile(candidate) if callable(self.reconcile) else self.reconcile
                )
                return json.dumps(payload)
            async with self.factory() as session:
                repository = DurableMemoryRepository(session)
                existing = await repository.active_by_fingerprint(
                    memory_fingerprint(candidate.content or "")
                )
                command = (
                    await session.execute(
                        select(MemoryCommandRow).where(
                            MemoryCommandRow.operation == "correct",
                            MemoryCommandRow.status == "pending",
                            MemoryCommandRow.target_memory_id.is_not(None),
                        )
                    )
                ).scalars().first()
            if existing is not None:
                return json.dumps({
                    "relation": "duplicate_of",
                    "target_memory_id": str(existing.id),
                    "replacement_content": None,
                })
            if command is not None:
                async with self.factory() as session:
                    target = await session.get(DurableMemoryRow, command.target_memory_id)
                # Only an active target can be superseded; a legacy correction
                # guard is a restriction, so the replacement is a new memory.
                if target is not None and target.status == "active":
                    return json.dumps({
                        "relation": "supersedes",
                        "target_memory_id": str(command.target_memory_id),
                        "replacement_content": candidate.content,
                    })
            return json.dumps({
                "relation": "new", "target_memory_id": None, "replacement_content": None,
            })
        raise AssertionError("unexpected model stage")


def _service(pg_factory, model, *, max_attempts: int = 3) -> MemoryReconciliationService:
    return MemoryReconciliationService(
        pg_factory, model, lease_seconds=30, max_attempts=max_attempts
    )


async def _rows(pg_factory, queued: dict) -> tuple[MemoryCandidateRow, MemoryObligationRow, MemoryCommandRow]:
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, UUID(queued["candidate_id"]))
        obligation = await session.get(MemoryObligationRow, UUID(queued["obligation_id"]))
        command = await session.get(MemoryCommandRow, UUID(queued["command_id"]))
        assert candidate is not None and obligation is not None and command is not None
        return candidate, obligation, command


async def _pending_review(lifecycle: MemoryLifecycleCommands, obligation_id: str) -> dict:
    pending = await lifecycle.obligations({"status": "pending"})
    return next(item for item in pending["obligations"] if item["obligation_id"] == obligation_id)


@pytest.mark.asyncio
async def test_explicit_remember_queues_candidate_and_pending_obligation(pg_factory):
    transcript_id, turn_id = await _seed_owner_turn(pg_factory, "Remember that I prefer strict tests.")
    queued = await MemoryService(pg_factory).remember({
        "content": "Jaco prefers strict tests.", "kind": "preference",
    })
    assert queued["status"] == "queued"
    assert queued["result"] == "queued_for_verification"
    assert "memory" not in queued

    candidate, obligation, command = await _rows(pg_factory, queued)
    assert candidate.status == "pending"
    assert candidate.origin == "explicit_owner_request"
    assert candidate.intake_path == "explicit_command"
    assert candidate.source_turn_id == turn_id
    assert obligation.kind == "explicit_remember"
    assert obligation.status == "pending"
    assert obligation.subject_type == "memory_candidate"
    assert obligation.command_id == command.id
    assert command.status == "pending"
    assert command.operation == "remember"
    assert command.source_transcript_id == transcript_id
    async with pg_factory() as session:
        assert (await session.execute(
            select(func.count()).select_from(DurableMemoryRow)
        )).scalar_one() == 0


@pytest.mark.asyncio
async def test_explicit_remember_intake_failure_terminates_visibly(pg_factory):
    # No owner turn exists, so there is no canonical evidence to bind.
    failed = await MemoryService(pg_factory).remember({"content": "Nothing can ground this."})
    assert failed["status"] == "failed"
    assert failed["result"] == "intake_failed"
    async with pg_factory() as session:
        obligation = (await session.execute(select(MemoryObligationRow))).scalar_one()
        command = (await session.execute(select(MemoryCommandRow))).scalar_one()
        assert obligation.status == "resolved"
        assert obligation.resolution_code == "intake_failed"
        assert obligation.subject_type == "memory_command"
        assert command.status == "failed"
        assert "canonical owner turn" in str(command.error)
        assert (await session.execute(
            select(func.count()).select_from(MemoryCandidateRow)
        )).scalar_one() == 0


@pytest.mark.asyncio
async def test_explicit_remember_obligation_resolves_with_memory_commit(pg_factory):
    _, turn_id = await _seed_owner_turn(pg_factory, "Remember that my workshop bird is a starling.")
    queued = await MemoryService(pg_factory).remember({"content": "My workshop bird is a starling."})
    result = await _service(pg_factory, _OwnerClaimModel(pg_factory)).run_once()
    assert result.reconciled == 1, result

    candidate, obligation, command = await _rows(pg_factory, queued)
    assert candidate.status == "reconciled"
    assert obligation.status == "resolved"
    assert obligation.resolution_code == "published"
    assert command.status == "applied"
    assert command.applied_at is not None
    memory_id = UUID(str(obligation.resolution_json["memory_id"]))
    assert command.replacement_memory_id == memory_id
    async with pg_factory() as session:
        memory = await session.get(DurableMemoryRow, memory_id)
        assert memory is not None
        assert memory.status == "active"
        assert memory.grounding_status == "verified"
        assert memory.originating_candidate_id == candidate.id
        assert memory.source_turn_id == turn_id


@pytest.mark.asyncio
async def test_explicit_remember_obligation_resolves_when_publication_is_blocked(pg_factory):
    await _seed_owner_turn(pg_factory, "Remember the project-local detail.")
    queued = await MemoryService(pg_factory).remember({"content": "The project-local detail."})
    # A project-scoped reading is a hard policy block; the publisher never runs.
    result = await _service(pg_factory, _OwnerClaimModel(pg_factory, scope="project")).run_once()
    assert result.blocked == 1, result

    candidate, obligation, command = await _rows(pg_factory, queued)
    assert candidate.status == "blocked"
    assert obligation.status == "resolved"
    assert obligation.resolution_code == "blocked"
    assert obligation.resolution_json["code"] == "project_identity_unresolved"
    assert command.status == "failed"
    assert command.error.startswith("blocked:")
    async with pg_factory() as session:
        assert (await session.execute(
            select(func.count()).select_from(DurableMemoryRow)
        )).scalar_one() == 0


@pytest.mark.asyncio
async def test_external_claim_behind_explicit_request_still_requires_owner_confirmation(pg_factory):
    await _seed_owner_turn(pg_factory, "Remember that the vendor says the part ships Monday.")
    queued = await MemoryService(pg_factory).remember({"content": "The vendor part ships Monday."})
    result = await _service(
        pg_factory, _OwnerClaimModel(pg_factory, claim_principal="external")
    ).run_once()
    assert result.blocked == 1, result
    candidate, obligation, command = await _rows(pg_factory, queued)
    assert candidate.status == "awaiting_owner"
    # The owner's storage intent is not evidence authority; the request stays open.
    assert obligation.status == "pending"
    assert command.status == "pending"
    async with pg_factory() as session:
        confirmation = (await session.execute(
            select(MemoryObligationRow).where(
                MemoryObligationRow.kind.in_({"memory_confirmation", "memory_review"})
            )
        )).scalar_one()
        assert confirmation.status == "pending"
    lifecycle = MemoryLifecycleCommands(pg_factory)
    review = await _pending_review(lifecycle, str(confirmation.id))
    rejected = await lifecycle.resolve_obligation({
        "obligation_id": str(confirmation.id),
        "decision": "reject",
        "review_version": review["review_version"],
    })
    assert rejected["result"] == "rejected"
    candidate, obligation, command = await _rows(pg_factory, queued)
    assert candidate.status == "rejected"
    assert obligation.status == "resolved"
    assert obligation.resolution_code == "rejected"
    assert command.status == "failed"


@pytest.mark.asyncio
async def test_failed_remember_attempt_keeps_obligation_pending_for_automatic_retry(pg_factory):
    await _seed_owner_turn(pg_factory, "Remember my rollback preference.")
    queued = await MemoryService(pg_factory).remember({"content": "Jaco prefers strict rollback tests."})
    failing = _OwnerClaimModel(pg_factory, fail_stage="blind")
    result = await _service(pg_factory, failing, max_attempts=3).run_once()
    assert result.failures == 1

    candidate, obligation, command = await _rows(pg_factory, queued)
    assert candidate.status == "pending"
    assert int(candidate.attempt_count) == 1
    assert obligation.status == "pending"
    assert "failure_count" not in (obligation.resolution_json or {})
    assert command.status == "pending"

    # Automatic retry: the next claim re-leases the same candidate without owner action.
    claims, _ = await MemoryCandidateLeaseRepository(
        pg_factory, lease_seconds=30, max_attempts=3
    ).claim_batch(limit=4)
    assert [claim.candidate_id for claim in claims] == [candidate.id]
    candidate, obligation, _ = await _rows(pg_factory, queued)
    assert candidate.status == "leased"
    assert obligation.status == "pending"
    async with pg_factory() as session:
        assert (await session.execute(
            select(func.count()).select_from(DurableMemoryRow)
        )).scalar_one() == 0


@pytest.mark.asyncio
async def test_failed_remember_transaction_rolls_back_memory_but_keeps_obligation_pending(
    pg_factory, monkeypatch
):
    await _seed_owner_turn(pg_factory, "Remember that I prefer strict rollback tests.")
    queued = await MemoryService(pg_factory).remember({"content": "Jaco prefers strict rollback tests."})

    async def fail_after_mutation(*_args, **_kwargs):
        raise RuntimeError("forced failure after memory mutation")

    monkeypatch.setattr(DurableMemoryRepository, "create_active", fail_after_mutation)
    result = await _service(pg_factory, _OwnerClaimModel(pg_factory), max_attempts=3).run_once()
    assert result.failures == 1
    candidate, obligation, command = await _rows(pg_factory, queued)
    assert candidate.status == "pending"
    assert obligation.status == "pending"
    assert obligation.resolution_code is None
    assert obligation.resolved_at is None
    assert command.status == "pending"
    async with pg_factory() as session:
        assert (await session.execute(
            select(func.count()).select_from(DurableMemoryRow)
        )).scalar_one() == 0


@pytest.mark.asyncio
async def test_failed_explicit_remember_retries_from_persisted_evidence(pg_factory):
    await _seed_owner_turn(pg_factory, "Remember that my workshop bird is a starling.")
    queued = await MemoryService(pg_factory).remember({"content": "My workshop bird is a starling."})
    failing = _OwnerClaimModel(pg_factory, fail_stage="blind")
    result = await _service(pg_factory, failing, max_attempts=1).run_once()
    assert result.failures == 1
    candidate, obligation, command = await _rows(pg_factory, queued)
    assert candidate.status == "failed"
    assert obligation.status == "pending"
    assert obligation.resolution_json["failure_count"] == 1
    failed_version = int(candidate.state_version)

    lifecycle = MemoryLifecycleCommands(pg_factory)
    review = await _pending_review(lifecycle, queued["obligation_id"])
    assert review["proposed_assertion_text"] == "My workshop bird is a starling."
    stale = await lifecycle.resolve_obligation({
        "obligation_id": queued["obligation_id"],
        "decision": "retry",
        "review_version": "not-the-current-version",
    })
    assert stale["result"] == "stale_review"
    with pytest.raises(ValueError):
        await lifecycle.resolve_obligation({
            "obligation_id": queued["obligation_id"],
            "decision": "confirm",
            "review_version": review["review_version"],
        })
    retried = await lifecycle.resolve_obligation({
        "obligation_id": queued["obligation_id"],
        "decision": "retry",
        "review_version": review["review_version"],
    })
    assert retried["result"] == "requeued_for_reconciliation"
    candidate, obligation, _ = await _rows(pg_factory, queued)
    assert candidate.status == "pending"
    assert int(candidate.attempt_count) == 0
    assert int(candidate.state_version) == failed_version + 1
    assert candidate.decision_json["decision"] == "owner_retry"
    assert obligation.status == "pending"
    assert obligation.resolution_json["owner_retries"] == 1

    # Replaying the same retry is rejected by the state-version CAS.
    replay = await lifecycle.resolve_obligation({
        "obligation_id": queued["obligation_id"],
        "decision": "retry",
        "review_version": review["review_version"],
    })
    assert replay["result"] == "already_queued"

    result = await _service(pg_factory, _OwnerClaimModel(pg_factory), max_attempts=1).run_once()
    assert result.reconciled == 1, result
    candidate, obligation, command = await _rows(pg_factory, queued)
    assert candidate.status == "reconciled"
    assert obligation.status == "resolved"
    assert obligation.resolution_code == "published"
    assert obligation.resolution_json["owner_retries"] == 1
    assert command.status == "applied"


@pytest.mark.asyncio
async def test_explicit_remember_failure_budget_terminates_obligation(pg_factory):
    await _seed_owner_turn(pg_factory, "Remember my rollback preference.")
    queued = await MemoryService(pg_factory).remember({"content": "Jaco prefers strict rollback tests."})
    lifecycle = MemoryLifecycleCommands(pg_factory)
    failing = _OwnerClaimModel(pg_factory, fail_stage="blind")
    for expected in range(1, OWNER_RETRY_BUDGET + 1):
        result = await _service(pg_factory, failing, max_attempts=1).run_once()
        assert result.failures == 1
        candidate, obligation, command = await _rows(pg_factory, queued)
        assert candidate.status == "failed"
        assert obligation.resolution_json["failure_count"] == expected
        if expected < OWNER_RETRY_BUDGET:
            assert obligation.status == "pending"
            assert command.status == "pending"
            review = await _pending_review(lifecycle, queued["obligation_id"])
            retried = await lifecycle.resolve_obligation({
                "obligation_id": queued["obligation_id"],
                "decision": "retry",
                "review_version": review["review_version"],
            })
            assert retried["result"] == "requeued_for_reconciliation"
    assert obligation.status == "resolved"
    assert obligation.resolution_code == "failed_retry_exhausted"
    assert obligation.resolved_at is not None
    assert command.status == "failed"
    assert command.error.startswith("failed_retry_exhausted")
    already = await lifecycle.resolve_obligation({
        "obligation_id": queued["obligation_id"], "decision": "retry",
    })
    assert already["result"] == "already_resolved"


@pytest.mark.asyncio
async def test_explicit_remember_obligation_resolves_when_evidence_is_purged(pg_factory):
    content = "The temporary code is ORBIT-7."
    await _seed_owner_turn(pg_factory, f"Remember this exact fact: {content}")
    service = MemoryService(pg_factory)
    queued = await service.remember({"content": content})
    candidate, obligation, command = await _rows(pg_factory, queued)
    assert obligation.status == "pending"

    deleted = await service.delete({"content": content})
    assert deleted["status"] == "applied"
    candidate, obligation, command = await _rows(pg_factory, queued)
    assert candidate.status == "invalidated"
    assert candidate.content is None
    assert obligation.status == "resolved"
    assert obligation.resolution_code == "evidence_purged"
    assert content not in json.dumps(obligation.resolution_json)
    assert command.status == "failed"
    assert command.error.startswith("evidence_purged")
    assert content not in str(command.error)


@pytest.mark.asyncio
async def test_explicit_correct_supersedes_target_through_publisher(pg_factory):
    transcript_id, _ = await _seed_owner_turn(pg_factory, "Remember that I live in London.")
    service = MemoryService(pg_factory)
    queued = await service.remember({"content": "I live in London."})
    assert (await _service(pg_factory, _OwnerClaimModel(pg_factory)).run_once()).reconciled == 1
    _, obligation, _ = await _rows(pg_factory, queued)
    old_id = str(obligation.resolution_json["memory_id"])

    async with pg_factory() as session:
        transcript = await session.get(TranscriptRow, transcript_id)
        transcript.next_turn_sequence = 2
        session.add(TurnRow(
            transcript_id=transcript_id, sequence=2, actor="owner",
            blocks=[{"type": "text", "text": "I moved and now live in Lisbon."}],
        ))
        await session.commit()
    corrected = await service.correct({
        "memory_id": old_id, "content": "I live in Lisbon.", "change_type": "change_over_time",
    })
    assert corrected["status"] == "queued"
    assert corrected["operation"] == "correct"
    candidate, obligation, command = await _rows(pg_factory, corrected)
    assert obligation.kind == "explicit_correct"
    assert candidate.origin == "explicit_owner_correction"
    assert command.target_memory_id == UUID(old_id)

    assert (await _service(pg_factory, _OwnerClaimModel(pg_factory)).run_once()).reconciled == 1
    candidate, obligation, command = await _rows(pg_factory, corrected)
    assert obligation.status == "resolved"
    assert obligation.resolution_code == "published"
    assert command.status == "applied"
    new_id = UUID(str(obligation.resolution_json["memory_id"]))
    assert command.replacement_memory_id == new_id
    async with pg_factory() as session:
        old = await session.get(DurableMemoryRow, UUID(old_id))
        new = await session.get(DurableMemoryRow, new_id)
        assert old.status == "superseded"
        assert old.superseded_by_id == new_id
        assert new.status == "active"
        assert new.supersedes_id == old.id
        assert new.originating_candidate_id == candidate.id


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", list(PublicationOutcome))
async def test_settlement_is_idempotent_across_publication_outcomes(pg_factory, outcome):
    """Settling reflects candidate terminality exactly once; replays are no-ops."""
    status_for_bucket = {
        "reconciled": "reconciled",
        "retained_short_term": "retained_short_term",
        "discarded": "discarded",
        "source_conflict": "pending",
        "lease_conflict": "pending",
        "version_conflict": "pending",
    }
    if outcome.bucket.value == "blocked":
        candidate_status = {
            "blocked": "blocked", "invalidated": "invalidated",
            "awaiting_owner": "awaiting_owner", "conflict": "awaiting_owner",
        }[outcome.value]
    else:
        candidate_status = status_for_bucket[outcome.bucket.value]
    transcript_id, turn_id = await _seed_owner_turn(pg_factory, "Remember the settlement fixture.")
    async with pg_factory() as session:
        command = MemoryCommandRow(
            operation="remember", status="pending", arguments_json={},
            source_transcript_id=transcript_id, source_turn_id=turn_id,
        )
        session.add(command)
        await session.flush()
        candidate = MemoryCandidateRow(
            status=candidate_status, kind="fact", content="The settlement fixture.",
            scope="cross_chat", confidence=1.0, durability="long_term",
            fingerprint="f" * 64, evidence_set_hash=uuid4().hex, origin="explicit_owner_request",
            intake_path="explicit_command", source_transcript_id=transcript_id,
            source_turn_id=turn_id, decision_json={"result": outcome.value, "code": "fixture"},
        )
        session.add(candidate)
        await session.flush()
        obligation = MemoryObligationRow(
            kind="explicit_remember", status="pending", subject_type="memory_candidate",
            subject_id=candidate.id, origin="explicit_owner_request", command_id=command.id,
            source_transcript_id=transcript_id, source_turn_id=turn_id,
        )
        session.add(obligation)
        await session.flush()
        now = datetime.now(UTC)
        first = await settle_explicit_obligation(session, candidate, now=now)
        assert first is obligation
        second = await settle_explicit_obligation(
            session, candidate, now=now + timedelta(seconds=1)
        )
        await session.commit()
        terminal = outcome.terminality in {
            PublicationTerminality.TERMINAL_SUCCESS, PublicationTerminality.TERMINAL_FAILURE,
        }
        if terminal:
            assert obligation.status == "resolved"
            assert obligation.resolved_at == now
            assert second is None
            assert command.status == ("applied" if candidate_status == "reconciled" else "failed")
        else:
            assert obligation.status == "pending"
            assert second is obligation
            assert command.status == "pending"


@pytest.mark.asyncio
async def test_explicit_outcomes_surface_once_into_foreground_context(pg_factory):
    transcript_id, _ = await _seed_owner_turn(pg_factory, "Remember that my workshop bird is a starling.")
    queued = await MemoryService(pg_factory).remember({"content": "My workshop bird is a starling."})
    lifecycle = MemoryLifecycleCommands(pg_factory)
    assert await lifecycle.explicit_outcomes_since(transcript_id) == []
    assert (await _service(pg_factory, _OwnerClaimModel(pg_factory)).run_once()).reconciled == 1

    outcomes = await lifecycle.explicit_outcomes_since(transcript_id)
    assert len(outcomes) == 1
    assert outcomes[0]["operation"] == "remember"
    assert outcomes[0]["resolution_code"] == "published"
    assert outcomes[0]["content"] == "My workshop bird is a starling."
    assert outcomes[0]["obligation_id"] == queued["obligation_id"]
    rendered = render_memory_outcomes(outcomes)
    assert "remember: published" in rendered
    assert outcomes[0]["memory_id"] in rendered
    assert "My workshop bird is a starling." in rendered

    # Once Atlas has replied after the resolution, the outcome is no longer surfaced.
    later = datetime.now(UTC) + timedelta(seconds=1)
    assert await lifecycle.explicit_outcomes_since(transcript_id, since=later) == []
    assert render_memory_outcomes([]) is None


async def _append_owner_turn(pg_factory, transcript_id: UUID, text: str) -> UUID:
    async with pg_factory() as session:
        transcript = await session.get(TranscriptRow, transcript_id)
        sequence = int(transcript.next_turn_sequence or 0) + 1
        transcript.next_turn_sequence = sequence
        turn = TurnRow(
            transcript_id=transcript_id, sequence=sequence, actor="owner",
            blocks=[{"type": "text", "text": text}],
        )
        session.add(turn)
        await session.commit()
        return turn.id


async def _publish_ok(pg_factory, model=None) -> None:
    result = await _service(pg_factory, model or _OwnerClaimModel(pg_factory)).run_once()
    assert result.reconciled == 1, result


async def _published_memory_id(pg_factory, queued: dict) -> UUID:
    _, obligation, _ = await _rows(pg_factory, queued)
    assert obligation.resolution_code == "published", obligation.resolution_code
    return UUID(str(obligation.resolution_json["memory_id"]))


async def _provenance(pg_factory, memory_id: UUID) -> list[tuple[str, UUID | None]]:
    async with pg_factory() as session:
        rows = list((await session.execute(
            select(MemoryProvenanceRow).where(MemoryProvenanceRow.memory_id == memory_id)
        )).scalars())
        return [(row.relationship, row.source_memory_id) for row in rows]


@pytest.mark.asyncio
async def test_legacy_correction_links_replacement_to_intake_guard(pg_factory):
    old, new = "My preferred bird is a starling.", "My preferred bird is a raven."
    await _seed_owner_turn(pg_factory, f"Correction: not a starling, {new}")
    service = MemoryService(pg_factory)
    corrected = await service.correct({"old_content": old, "content": new})
    assert corrected["status"] == "queued"
    # The challenged wording stops shaping context immediately, before verification.
    assert corrected["_context_suppression"]["contents"] == [old]

    _, obligation, command = await _rows(pg_factory, corrected)
    assert obligation.status == "pending"
    guard_id = command.target_memory_id
    assert guard_id is not None
    async with pg_factory() as session:
        guard = await session.get(DurableMemoryRow, guard_id)
        assert guard.record_kind == "owner_correction_guard"
        assert guard.status == "superseded"
        assert guard.suppresses_recall is True
        assert guard.grounding_status == "verified"
        assert guard.owner_assertion_turn_id is not None
        assert guard.content == old
        assert guard.superseded_by_id is None
        guards = await DurableMemoryRepository(session).recall_guards()
        assert old in {item["content"] for item in guards}

    await _publish_ok(pg_factory)
    new_id = await _published_memory_id(pg_factory, corrected)
    async with pg_factory() as session:
        memory = await session.get(DurableMemoryRow, new_id)
        guard = await session.get(DurableMemoryRow, guard_id)
        assert memory.status == "active"
        assert memory.content == new
        assert memory.supersedes_id == guard_id
        assert guard.superseded_by_id == new_id
        assert memory.valid_from is None
    assert ("corrects", guard_id) in await _provenance(pg_factory, new_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change_type", "relationship"),
    [("correction", "corrects"), ("change_over_time", "supersedes")],
)
async def test_correction_change_type_controls_validity_window(pg_factory, change_type, relationship):
    transcript_id, _ = await _seed_owner_turn(pg_factory, "Remember that I live in London.")
    service = MemoryService(pg_factory)
    queued = await service.remember({"content": "I live in London."})
    await _publish_ok(pg_factory)
    old_id = await _published_memory_id(pg_factory, queued)
    await _append_owner_turn(pg_factory, transcript_id, "Actually, I live in Lisbon.")

    corrected = await service.correct({
        "memory_id": str(old_id), "content": "I live in Lisbon.", "change_type": change_type,
    })
    assert corrected["_context_suppression"]["contents"] == ["I live in London."]
    _, _, command = await _rows(pg_factory, corrected)
    assert command.target_memory_id == old_id
    await _publish_ok(pg_factory)
    new_id = await _published_memory_id(pg_factory, corrected)

    async with pg_factory() as session:
        old = await session.get(DurableMemoryRow, old_id)
        new = await session.get(DurableMemoryRow, new_id)
        assert old.status == "superseded"
        assert old.suppresses_recall is True
        assert old.superseded_by_id == new_id
        assert new.status == "active"
        assert new.supersedes_id == old_id
        assert old.valid_from is not None
        if change_type == "correction":
            # A wrong claim was never true: no validity window is closed or opened.
            assert old.valid_to is None
            assert new.valid_from is None
        else:
            assert old.valid_to is not None
            assert new.valid_from is not None
            assert new.valid_from >= old.valid_from
    assert (relationship, old_id) in await _provenance(pg_factory, new_id)


@pytest.mark.asyncio
async def test_pending_correction_suppresses_target_from_foreground_context(pg_factory):
    from atlas.api.app import _active_memory_suppression_contents

    transcript_id, _ = await _seed_owner_turn(pg_factory, "Remember that I live in London.")
    service = MemoryService(pg_factory)
    queued = await service.remember({"content": "I live in London."})
    await _publish_ok(pg_factory)
    old_id = await _published_memory_id(pg_factory, queued)
    async with pg_factory() as session:
        assert await _active_memory_suppression_contents(session) == []

    await _append_owner_turn(pg_factory, transcript_id, "Actually, I live in Lisbon.")
    corrected = await service.correct({"memory_id": str(old_id), "content": "I live in Lisbon."})
    async with pg_factory() as session:
        # Pending correction: the challenged claim is suppressed without any durable change.
        assert await _active_memory_suppression_contents(session) == ["I live in London."]
        old = await session.get(DurableMemoryRow, old_id)
        assert old.status == "active"
        assert old.suppresses_recall is False

    await _publish_ok(pg_factory)
    async with pg_factory() as session:
        # Published correction: suppression now comes from the superseded row itself.
        assert await _active_memory_suppression_contents(session) == ["I live in London."]
        old = await session.get(DurableMemoryRow, old_id)
        assert old.status == "superseded"
        assert old.suppresses_recall is True
    _, obligation, command = await _rows(pg_factory, corrected)
    assert obligation.resolution_code == "published"
    assert command.status == "applied"


@pytest.mark.asyncio
async def test_explicit_correction_target_mismatch_is_blocked(pg_factory):
    transcript_id, _ = await _seed_owner_turn(pg_factory, "Remember that I live in London.")
    service = MemoryService(pg_factory)
    first = await service.remember({"content": "I live in London."})
    await _publish_ok(pg_factory)
    target_id = await _published_memory_id(pg_factory, first)
    await _append_owner_turn(pg_factory, transcript_id, "Remember that I work in Cambridge.")
    other = await service.remember({"content": "I work in Cambridge."})
    await _publish_ok(pg_factory)
    other_id = await _published_memory_id(pg_factory, other)

    await _append_owner_turn(pg_factory, transcript_id, "Actually, I live in Lisbon.")
    corrected = await service.correct({"memory_id": str(target_id), "content": "I live in Lisbon."})
    # The verifier tries to supersede a different memory than the owner named.
    drifting = _OwnerClaimModel(pg_factory, reconcile=lambda candidate: {
        "relation": "supersedes",
        "target_memory_id": str(other_id),
        "replacement_content": candidate.content,
    })
    result = await _service(pg_factory, drifting).run_once()
    assert result.blocked == 1, result

    candidate, obligation, command = await _rows(pg_factory, corrected)
    assert candidate.status == "blocked"
    assert candidate.decision_json["code"] == "explicit_correction_target_mismatch"
    assert obligation.resolution_code == "blocked"
    assert command.status == "failed"
    async with pg_factory() as session:
        for memory_id in (target_id, other_id):
            row = await session.get(DurableMemoryRow, memory_id)
            assert row.status == "active"
            assert row.superseded_by_id is None
        assert (await session.execute(
            select(func.count()).select_from(DurableMemoryRow)
        )).scalar_one() == 2


@pytest.mark.asyncio
async def test_explicit_correction_with_active_target_cannot_publish_as_new(pg_factory):
    transcript_id, _ = await _seed_owner_turn(pg_factory, "Remember that I live in London.")
    service = MemoryService(pg_factory)
    first = await service.remember({"content": "I live in London."})
    await _publish_ok(pg_factory)
    target_id = await _published_memory_id(pg_factory, first)
    await _append_owner_turn(pg_factory, transcript_id, "Actually, I live in Lisbon.")
    corrected = await service.correct({"memory_id": str(target_id), "content": "I live in Lisbon."})

    # A correction that ignores its named target cannot silently become a second fact.
    ignoring = _OwnerClaimModel(pg_factory, reconcile={
        "relation": "new", "target_memory_id": None, "replacement_content": None,
    })
    result = await _service(pg_factory, ignoring).run_once()
    assert result.blocked == 1, result
    candidate, obligation, command = await _rows(pg_factory, corrected)
    assert candidate.decision_json["code"] == "explicit_correction_target_not_reconciled"
    assert obligation.resolution_code == "blocked"
    assert command.status == "failed"
    async with pg_factory() as session:
        target = await session.get(DurableMemoryRow, target_id)
        assert target.status == "active"
        assert (await session.execute(
            select(func.count()).select_from(DurableMemoryRow)
        )).scalar_one() == 1
