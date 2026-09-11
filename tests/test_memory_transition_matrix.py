from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum
from itertools import product
from pathlib import Path

from atlas.memory.state_machine import (
    CANDIDATE_EVENT_PRODUCERS,
    REMEMBER_EVENT_PRODUCERS,
    CandidateEvent,
    CandidateState,
    RememberObligationEvent,
    RememberObligationState,
)


@dataclass(frozen=True)
class Coverage:
    test_name: str
    next_state: str


def _test_names() -> set[str]:
    names: set[str] = set()
    for path in Path("tests").glob("test_*.py"):
        tree = ast.parse(path.read_text())
        names.update(
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
    return names

CANDIDATE_COVERAGE: dict[tuple[CandidateState, CandidateEvent], Coverage] = {
    (CandidateState.ABSENT, CandidateEvent.INTAKE_ACCEPTED): Coverage(
        "test_ephemeral_handle_binding_is_committed_at_intake", "pending"
    ),
    (CandidateState.PENDING, CandidateEvent.CLAIM): Coverage(
        "test_end_to_end_candidate_reconciliation_creates_derived_memory", "leased"
    ),
    (CandidateState.PENDING, CandidateEvent.EVIDENCE_PURGED): Coverage(
        "test_purge_invalidates_candidate_from_every_persisted_state", "invalidated"
    ),
    (CandidateState.PENDING, CandidateEvent.EVIDENCE_UNAVAILABLE): Coverage(
        "test_owner_delete_during_inference_wins_and_invalidates_candidate", "invalidated"
    ),
    (CandidateState.PENDING, CandidateEvent.ATTEMPT_LIMIT_REACHED): Coverage(
        "test_expired_final_lease_is_failed_instead_of_sticking_forever", "failed"
    ),
    (CandidateState.LEASED, CandidateEvent.BLIND_READ_PERSISTED): Coverage(
        "test_blind_reading_is_persisted_before_proposal_exposure", "leased"
    ),
    (CandidateState.LEASED, CandidateEvent.COMPARISON_PERSISTED): Coverage(
        "test_blind_reading_is_persisted_before_proposal_exposure", "leased"
    ),
    (CandidateState.LEASED, CandidateEvent.RECONCILIATION_PERSISTED): Coverage(
        "test_older_event_discovered_late_becomes_historical_predecessor", "leased"
    ),
    (CandidateState.LEASED, CandidateEvent.POLICY_ALLOWS): Coverage(
        "test_end_to_end_candidate_reconciliation_creates_derived_memory", "leased"
    ),
    (CandidateState.LEASED, CandidateEvent.POLICY_REQUIRES_OWNER): Coverage(
        "test_owner_confirmation_reconciles_again_against_current_graph", "awaiting_owner"
    ),
    (CandidateState.LEASED, CandidateEvent.POLICY_BLOCKS): Coverage(
        "test_unresolved_project_scope_cannot_publish_durable_memory", "blocked"
    ),
    (CandidateState.LEASED, CandidateEvent.PUBLICATION_SUCCEEDS): Coverage(
        "test_end_to_end_candidate_reconciliation_creates_derived_memory", "reconciled"
    ),
    (CandidateState.LEASED, CandidateEvent.PUBLICATION_VERSION_CONFLICT): Coverage(
        "test_cas_conflict_reconciles_again_without_rerunning_blind_read", "pending"
    ),
    (CandidateState.LEASED, CandidateEvent.PUBLICATION_SOURCE_CONFLICT): Coverage(
        "test_source_revision_change_after_snapshot_requeues_without_publication", "pending"
    ),
    (CandidateState.LEASED, CandidateEvent.PUBLICATION_LEASE_CONFLICT): Coverage(
        "test_stale_lease_holder_cannot_publish_when_memory_revision_is_unchanged", "leased"
    ),
    (CandidateState.LEASED, CandidateEvent.PUBLICATION_SERIALIZATION_FAILURE): Coverage(
        "test_publication_serialization_failure_routes_to_reconciliation_retry", "pending"
    ),
    (CandidateState.LEASED, CandidateEvent.VERIFIER_FAILURE): Coverage(
        "test_verifier_timeout_requeues_and_next_lease_recovers", "pending"
    ),
    (CandidateState.LEASED, CandidateEvent.LEASE_EXPIRES): Coverage(
        "test_expired_lease_is_reclaimed_after_worker_disappears", "leased"
    ),
    (CandidateState.LEASED, CandidateEvent.EVIDENCE_PURGED): Coverage(
        "test_purge_invalidates_candidate_from_every_persisted_state", "invalidated"
    ),
    (CandidateState.LEASED, CandidateEvent.EVIDENCE_UNAVAILABLE): Coverage(
        "test_owner_delete_during_inference_wins_and_invalidates_candidate", "invalidated"
    ),
    (CandidateState.LEASED, CandidateEvent.ATTEMPT_LIMIT_REACHED): Coverage(
        "test_expired_final_lease_is_failed_instead_of_sticking_forever", "failed"
    ),
    (CandidateState.RETAINED_SHORT_TERM, CandidateEvent.RETENTION_REVIEW_DUE): Coverage(
        "test_short_term_retention_is_not_durable_memory_and_remains_deduplicated", "leased"
    ),
    (CandidateState.RETAINED_SHORT_TERM, CandidateEvent.RETENTION_EXPIRES): Coverage(
        "test_owner_confirmation_obligation_expires_visibly", "expired"
    ),
    (CandidateState.RETAINED_SHORT_TERM, CandidateEvent.EVIDENCE_PURGED): Coverage(
        "test_purge_invalidates_candidate_from_every_persisted_state", "invalidated"
    ),
    (CandidateState.AWAITING_OWNER, CandidateEvent.OWNER_CONFIRMS): Coverage(
        "test_owner_confirmation_reconciles_again_against_current_graph", "pending"
    ),
    (CandidateState.AWAITING_OWNER, CandidateEvent.OWNER_REJECTS): Coverage(
        "test_owner_rejection_terminates_confirmation_obligation", "rejected"
    ),
    (CandidateState.AWAITING_OWNER, CandidateEvent.OBLIGATION_EXPIRES): Coverage(
        "test_owner_confirmation_obligation_expires_visibly", "expired"
    ),
    (CandidateState.AWAITING_OWNER, CandidateEvent.EVIDENCE_PURGED): Coverage(
        "test_purge_invalidates_candidate_from_every_persisted_state", "invalidated"
    ),
}

for _terminal_state in (
    CandidateState.RECONCILED,
    CandidateState.DISCARDED,
    CandidateState.BLOCKED,
    CandidateState.INVALIDATED,
    CandidateState.EXPIRED,
    CandidateState.FAILED,
    CandidateState.REJECTED,
):
    CANDIDATE_COVERAGE[(_terminal_state, CandidateEvent.EVIDENCE_PURGED)] = Coverage(
        "test_purge_invalidates_candidate_from_every_persisted_state", "invalidated"
    )


REMEMBER_COVERAGE: dict[
    tuple[RememberObligationState, RememberObligationEvent], Coverage
] = {
    (RememberObligationState.ABSENT, RememberObligationEvent.INTAKE_CREATED): Coverage(
        "test_explicit_remember_queues_candidate_and_pending_obligation", "pending"
    ),
    (
        RememberObligationState.ABSENT,
        RememberObligationEvent.INTAKE_FAILS_BEFORE_EVIDENCE,
    ): Coverage(
        "test_explicit_remember_intake_failure_terminates_visibly", "resolved"
    ),
    (RememberObligationState.PENDING, RememberObligationEvent.PERSIST_SUCCEEDS): Coverage(
        "test_explicit_remember_obligation_resolves_with_memory_commit", "resolved"
    ),
    (
        RememberObligationState.PENDING,
        RememberObligationEvent.PUBLICATION_TERMINALLY_FAILS,
    ): Coverage(
        "test_explicit_remember_obligation_resolves_when_publication_is_blocked", "resolved"
    ),
    (
        RememberObligationState.PENDING,
        RememberObligationEvent.PERSIST_FAILS_AFTER_INTAKE,
    ): Coverage(
        "test_failed_remember_transaction_rolls_back_memory_but_keeps_obligation_pending",
        "pending",
    ),
    (RememberObligationState.PENDING, RememberObligationEvent.AUTOMATIC_RETRY): Coverage(
        "test_failed_remember_attempt_keeps_obligation_pending_for_automatic_retry", "pending"
    ),
    (
        RememberObligationState.PENDING,
        RememberObligationEvent.RETRY_BUDGET_EXHAUSTED,
    ): Coverage(
        "test_explicit_remember_failure_budget_terminates_obligation", "resolved"
    ),
    (RememberObligationState.PENDING, RememberObligationEvent.OWNER_RETRY): Coverage(
        "test_failed_explicit_remember_retries_from_persisted_evidence", "pending"
    ),
    (RememberObligationState.PENDING, RememberObligationEvent.EVIDENCE_PURGED): Coverage(
        "test_explicit_remember_obligation_resolves_when_evidence_is_purged", "resolved"
    ),
}


CANDIDATE_ALLOWED_EVENTS: dict[CandidateState, set[CandidateEvent]] = {
    CandidateState.ABSENT: {CandidateEvent.INTAKE_ACCEPTED},
    CandidateState.PENDING: {
        CandidateEvent.CLAIM, CandidateEvent.EVIDENCE_PURGED,
        CandidateEvent.EVIDENCE_UNAVAILABLE, CandidateEvent.ATTEMPT_LIMIT_REACHED,
    },
    CandidateState.LEASED: {
        CandidateEvent.BLIND_READ_PERSISTED, CandidateEvent.COMPARISON_PERSISTED,
        CandidateEvent.RECONCILIATION_PERSISTED, CandidateEvent.POLICY_ALLOWS,
        CandidateEvent.POLICY_REQUIRES_OWNER, CandidateEvent.POLICY_BLOCKS,
        CandidateEvent.PUBLICATION_SUCCEEDS, CandidateEvent.PUBLICATION_VERSION_CONFLICT,
        CandidateEvent.PUBLICATION_SOURCE_CONFLICT, CandidateEvent.PUBLICATION_LEASE_CONFLICT,
        CandidateEvent.PUBLICATION_SERIALIZATION_FAILURE, CandidateEvent.VERIFIER_FAILURE,
        CandidateEvent.LEASE_EXPIRES, CandidateEvent.EVIDENCE_PURGED,
        CandidateEvent.EVIDENCE_UNAVAILABLE, CandidateEvent.ATTEMPT_LIMIT_REACHED,
    },
    CandidateState.RETAINED_SHORT_TERM: {
        CandidateEvent.RETENTION_REVIEW_DUE, CandidateEvent.RETENTION_EXPIRES,
        CandidateEvent.EVIDENCE_PURGED,
    },
    CandidateState.AWAITING_OWNER: {
        CandidateEvent.OWNER_CONFIRMS, CandidateEvent.OWNER_REJECTS,
        CandidateEvent.OBLIGATION_EXPIRES, CandidateEvent.EVIDENCE_PURGED,
    },
}
for _state in (
    CandidateState.RECONCILED, CandidateState.DISCARDED, CandidateState.BLOCKED,
    CandidateState.INVALIDATED, CandidateState.EXPIRED, CandidateState.FAILED,
    CandidateState.REJECTED,
):
    CANDIDATE_ALLOWED_EVENTS[_state] = {CandidateEvent.EVIDENCE_PURGED}

REMEMBER_ALLOWED_EVENTS: dict[RememberObligationState, set[RememberObligationEvent]] = {
    RememberObligationState.ABSENT: {
        RememberObligationEvent.INTAKE_CREATED,
        RememberObligationEvent.INTAKE_FAILS_BEFORE_EVIDENCE,
    },
    RememberObligationState.PENDING: {
        RememberObligationEvent.PERSIST_SUCCEEDS,
        RememberObligationEvent.PUBLICATION_TERMINALLY_FAILS,
        RememberObligationEvent.PERSIST_FAILS_AFTER_INTAKE,
        RememberObligationEvent.AUTOMATIC_RETRY,
        RememberObligationEvent.RETRY_BUDGET_EXHAUSTED,
        RememberObligationEvent.OWNER_RETRY,
        RememberObligationEvent.EVIDENCE_PURGED,
    },
    RememberObligationState.RESOLVED: set(),
}


def _assert_impossible(*, state: StrEnum, event: StrEnum, producer: str | None) -> None:
    # The producer annotation is global for the event even when a particular
    # state/event pair is impossible. A missing producer is therefore still a
    # machine defect, not an impossible-pair exemption.
    assert producer, f"{event.value} has no event producer; {state.value} cannot be classified"
    if isinstance(state, CandidateState):
        assert isinstance(event, CandidateEvent)
        assert event not in CANDIDATE_ALLOWED_EVENTS[state], (
            f"possible transition {state.value} + {event.value} lacks behavioral coverage"
        )
    else:
        assert isinstance(state, RememberObligationState)
        assert isinstance(event, RememberObligationEvent)
        assert event not in REMEMBER_ALLOWED_EVENTS[state], (
            f"possible transition {state.value} + {event.value} lacks behavioral coverage"
        )


def test_candidate_state_event_matrix_is_exhaustive() -> None:
    tests = _test_names()
    assert set(CANDIDATE_EVENT_PRODUCERS) == set(CandidateEvent)
    for state, event in product(CandidateState, CandidateEvent):
        coverage = CANDIDATE_COVERAGE.get((state, event))
        producer = CANDIDATE_EVENT_PRODUCERS.get(event)
        if coverage is None:
            _assert_impossible(state=state, event=event, producer=producer)
            continue
        assert producer, f"possible transition {state.value} + {event.value} has no producer"
        assert coverage.test_name in tests, (
            f"possible transition {state.value} + {event.value} names missing behavioral "
            f"test {coverage.test_name}"
        )


def test_remember_obligation_state_event_matrix_is_exhaustive() -> None:
    tests = _test_names()
    assert set(REMEMBER_EVENT_PRODUCERS) == set(RememberObligationEvent)
    for state, event in product(RememberObligationState, RememberObligationEvent):
        coverage = REMEMBER_COVERAGE.get((state, event))
        producer = REMEMBER_EVENT_PRODUCERS.get(event)
        if coverage is None:
            _assert_impossible(state=state, event=event, producer=producer)
            continue
        assert producer, f"possible transition {state.value} + {event.value} has no producer"
        assert coverage.test_name in tests, (
            f"possible transition {state.value} + {event.value} names missing behavioral "
            f"test {coverage.test_name}"
        )
