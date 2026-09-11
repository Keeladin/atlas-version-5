from __future__ import annotations

from enum import Enum, StrEnum


class CandidateState(StrEnum):
    ABSENT = "absent"
    PENDING = "pending"
    LEASED = "leased"
    RETAINED_SHORT_TERM = "retained_short_term"
    AWAITING_OWNER = "awaiting_owner"
    RECONCILED = "reconciled"
    DISCARDED = "discarded"
    BLOCKED = "blocked"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    FAILED = "failed"
    REJECTED = "rejected"


class CandidateEvent(StrEnum):
    INTAKE_ACCEPTED = "intake_accepted"
    CLAIM = "claim"
    BLIND_READ_PERSISTED = "blind_read_persisted"
    COMPARISON_PERSISTED = "comparison_persisted"
    RECONCILIATION_PERSISTED = "reconciliation_persisted"
    POLICY_ALLOWS = "policy_allows"
    POLICY_REQUIRES_OWNER = "policy_requires_owner"
    POLICY_BLOCKS = "policy_blocks"
    PUBLICATION_SUCCEEDS = "publication_succeeds"
    PUBLICATION_VERSION_CONFLICT = "publication_version_conflict"
    PUBLICATION_SOURCE_CONFLICT = "publication_source_conflict"
    PUBLICATION_LEASE_CONFLICT = "publication_lease_conflict"
    PUBLICATION_SERIALIZATION_FAILURE = "publication_serialization_failure"
    VERIFIER_FAILURE = "verifier_failure"
    LEASE_EXPIRES = "lease_expires"
    EVIDENCE_PURGED = "evidence_purged"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    RETENTION_REVIEW_DUE = "retention_review_due"
    RETENTION_EXPIRES = "retention_expires"
    OWNER_CONFIRMS = "owner_confirms"
    OWNER_REJECTS = "owner_rejects"
    OBLIGATION_EXPIRES = "obligation_expires"
    ATTEMPT_LIMIT_REACHED = "attempt_limit_reached"

class PublicationBucket(StrEnum):
    RECONCILED = "reconciled"
    RETAINED_SHORT_TERM = "retained_short_term"
    DISCARDED = "discarded"
    BLOCKED = "blocked"
    SOURCE_CONFLICT = "source_conflict"
    LEASE_CONFLICT = "lease_conflict"
    VERSION_CONFLICT = "version_conflict"


class PublicationTerminality(StrEnum):
    TERMINAL_SUCCESS = "terminal_success"
    TERMINAL_FAILURE = "terminal_failure"
    NONTERMINAL = "nonterminal"
    RETRY = "retry"


class PublicationOutcome(str, Enum):
    CREATED = ("created", PublicationBucket.RECONCILED, PublicationTerminality.TERMINAL_SUCCESS)
    MERGE = ("merge", PublicationBucket.RECONCILED, PublicationTerminality.TERMINAL_SUCCESS)
    SUPERSEDE = ("supersede", PublicationBucket.RECONCILED, PublicationTerminality.TERMINAL_SUCCESS)
    HISTORICAL_PREDECESSOR = ("historical_predecessor", PublicationBucket.RECONCILED, PublicationTerminality.TERMINAL_SUCCESS)
    HISTORICAL_LINEAGE_ADDED = ("historical_lineage_added", PublicationBucket.RECONCILED, PublicationTerminality.TERMINAL_SUCCESS)
    HISTORICAL_ALREADY_REPRESENTED = ("historical_already_represented", PublicationBucket.RECONCILED, PublicationTerminality.TERMINAL_SUCCESS)
    LEGACY_GROUNDED = ("legacy_grounded", PublicationBucket.RECONCILED, PublicationTerminality.TERMINAL_SUCCESS)
    LINEAGE_ADDED = ("lineage_added", PublicationBucket.RECONCILED, PublicationTerminality.TERMINAL_SUCCESS)
    ALREADY_REPRESENTED = ("already_represented", PublicationBucket.RECONCILED, PublicationTerminality.TERMINAL_SUCCESS)
    RETAINED_SHORT_TERM = ("retained_short_term", PublicationBucket.RETAINED_SHORT_TERM, PublicationTerminality.NONTERMINAL)
    DISCARDED = ("discarded", PublicationBucket.DISCARDED, PublicationTerminality.TERMINAL_SUCCESS)
    BLOCKED = ("blocked", PublicationBucket.BLOCKED, PublicationTerminality.TERMINAL_FAILURE)
    INVALIDATED = ("invalidated", PublicationBucket.BLOCKED, PublicationTerminality.TERMINAL_FAILURE)
    AWAITING_OWNER = ("awaiting_owner", PublicationBucket.BLOCKED, PublicationTerminality.NONTERMINAL)
    CONFLICT = ("conflict", PublicationBucket.BLOCKED, PublicationTerminality.NONTERMINAL)
    SOURCE_CONFLICT = ("source_conflict", PublicationBucket.SOURCE_CONFLICT, PublicationTerminality.RETRY)
    LEASE_CONFLICT = ("lease_conflict", PublicationBucket.LEASE_CONFLICT, PublicationTerminality.RETRY)
    VERSION_CONFLICT = ("version_conflict", PublicationBucket.VERSION_CONFLICT, PublicationTerminality.RETRY)

    def __new__(cls, value: str, bucket: PublicationBucket, terminality: PublicationTerminality):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.bucket = bucket
        obj.terminality = terminality
        return obj


class RememberObligationState(StrEnum):
    ABSENT = "absent"
    PENDING = "pending"
    RESOLVED = "resolved"


class RememberObligationEvent(StrEnum):
    INTAKE_CREATED = "intake_created"
    PERSIST_SUCCEEDS = "persist_succeeds"
    PERSIST_FAILS_AFTER_INTAKE = "persist_fails_after_intake"
    OWNER_RETRY = "owner_retry"
    INTAKE_FAILS_BEFORE_EVIDENCE = "intake_fails_before_evidence"
    EVIDENCE_PURGED = "evidence_purged"


CANDIDATE_EVENT_PRODUCERS: dict[CandidateEvent, str] = {
    CandidateEvent.INTAKE_ACCEPTED: "MemoryCandidateIntake.enqueue_many",
    CandidateEvent.CLAIM: "MemoryCandidateLeaseRepository.claim_batch",
    CandidateEvent.BLIND_READ_PERSISTED: "MemoryReconciliationService._persist_reading",
    CandidateEvent.COMPARISON_PERSISTED: "MemoryReconciliationService._persist_comparison",
    CandidateEvent.RECONCILIATION_PERSISTED: "MemoryReconciliationService._persist_reconciliation",
    CandidateEvent.POLICY_ALLOWS: "MemoryReconciliationService._persist_policy",
    CandidateEvent.POLICY_REQUIRES_OWNER: "MemoryReconciliationService._persist_policy",
    CandidateEvent.POLICY_BLOCKS: "MemoryReconciliationService._persist_policy",
    CandidateEvent.PUBLICATION_SUCCEEDS: "DerivedMemoryPublisher.publish",
    CandidateEvent.PUBLICATION_VERSION_CONFLICT: "SharedWriteRepository.execute",
    CandidateEvent.PUBLICATION_SOURCE_CONFLICT: "DerivedMemoryPublisher.publish",
    CandidateEvent.PUBLICATION_LEASE_CONFLICT: "DerivedMemoryPublisher.publish",
    CandidateEvent.PUBLICATION_SERIALIZATION_FAILURE: "SharedWriteRepository.execute",
    CandidateEvent.VERIFIER_FAILURE: "MemoryReconciliationService.run_once",
    CandidateEvent.LEASE_EXPIRES: "MemoryCandidateLeaseRepository.claim_batch",
    CandidateEvent.EVIDENCE_PURGED: "MemoryLifecycleCommands.delete",
    CandidateEvent.EVIDENCE_UNAVAILABLE: "MemoryCandidateLeaseRepository.claim_batch",
    CandidateEvent.RETENTION_REVIEW_DUE: "MemoryCandidateLeaseRepository.claim_batch",
    CandidateEvent.RETENTION_EXPIRES: "MemoryCandidateLeaseRepository.claim_batch",
    CandidateEvent.OWNER_CONFIRMS: "MemoryLifecycleCommands.resolve_obligation",
    CandidateEvent.OWNER_REJECTS: "MemoryLifecycleCommands.resolve_obligation",
    CandidateEvent.OBLIGATION_EXPIRES: "MemoryCandidateLeaseRepository._expire_due_obligations",
    CandidateEvent.ATTEMPT_LIMIT_REACHED: "MemoryCandidateLeaseRepository.claim_batch",
}


REMEMBER_EVENT_PRODUCERS: dict[RememberObligationEvent, str | None] = {
    RememberObligationEvent.INTAKE_CREATED: "MemoryLifecycleCommands._begin",
    RememberObligationEvent.PERSIST_SUCCEEDS: "MemoryLifecycleCommands._apply_command",
    RememberObligationEvent.PERSIST_FAILS_AFTER_INTAKE: "MemoryLifecycleCommands._fail",
    # Deliberately None until an owner retry can re-enter from persisted evidence.
    RememberObligationEvent.OWNER_RETRY: None,
    RememberObligationEvent.INTAKE_FAILS_BEFORE_EVIDENCE: "MemoryLifecycleCommands.remember",
    RememberObligationEvent.EVIDENCE_PURGED: "MemoryLifecycleCommands.delete",
}
