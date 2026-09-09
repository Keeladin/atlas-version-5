from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas.persistence.models import MemoryCandidateRow


class MemoryCandidate(BaseModel):
    """A model-proposed memory item. Runtime owns provenance and persistence."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "identity", "preference", "fact", "decision", "relationship",
        "procedure", "project_state", "intent",
    ]
    content: str = Field(min_length=1, max_length=2_000)
    scope: Literal["chat", "project", "cross_chat"]
    confidence: float = Field(ge=0.0, le=1.0)
    durability: Literal["short_term", "long_term"]
    proposed_action: Literal["upsert"] = "upsert"
    subject: str | None = Field(default=None, max_length=160)
    namespace: str | None = Field(default=None, max_length=160)
    evidence: str | None = Field(default=None, max_length=800)

    def normalized(self) -> MemoryCandidate:
        data = self.model_dump()
        for key in ("content", "subject", "namespace", "evidence"):
            value = data.get(key)
            if isinstance(value, str):
                data[key] = " ".join(value.split()).strip() or None
        if not data["content"]:
            raise ValueError("candidate content cannot be empty")
        return MemoryCandidate.model_validate(data)


def candidate_scope_key(candidate: MemoryCandidate, source_transcript_id: UUID) -> str:
    if candidate.scope == "cross_chat":
        return "owner"
    if candidate.scope == "chat":
        return f"chat:{source_transcript_id}"
    # V5 does not yet expose one canonical project identity to memory intake.
    # Keep project candidates isolated to their source chat until runtime can bind one.
    return f"project-unresolved:{source_transcript_id}"


def candidate_fingerprint(candidate: MemoryCandidate, scope_key: str) -> str:
    payload = {
        "kind": candidate.kind,
        "content": candidate.content.casefold(),
        "scope": candidate.scope,
        "scope_key": scope_key,
        "subject": (candidate.subject or "").casefold(),
        "namespace": (candidate.namespace or "").casefold(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class MemoryCandidateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self,
        candidate: MemoryCandidate,
        *,
        source_transcript_id: UUID,
        source_turn_id: UUID,
        source_provider_evidence_id: UUID | None,
    ) -> tuple[MemoryCandidateRow, bool]:
        candidate = candidate.normalized()
        scope_key = candidate_scope_key(candidate, source_transcript_id)
        fingerprint = candidate_fingerprint(candidate, scope_key)
        candidate_id = uuid4()
        statement = (
            insert(MemoryCandidateRow)
            .values(
                id=candidate_id,
                status="pending",
                kind=candidate.kind,
                content=candidate.content,
                scope=candidate.scope,
                scope_key=scope_key,
                confidence=candidate.confidence,
                durability=candidate.durability,
                proposed_action=candidate.proposed_action,
                subject=candidate.subject,
                namespace=candidate.namespace,
                evidence=candidate.evidence,
                fingerprint=fingerprint,
                source_transcript_id=source_transcript_id,
                source_turn_id=source_turn_id,
                source_provider_evidence_id=source_provider_evidence_id,
            )
            .on_conflict_do_nothing(
                index_elements=[MemoryCandidateRow.fingerprint],
                index_where=(MemoryCandidateRow.status == "pending")
                & MemoryCandidateRow.fingerprint.is_not(None),
            )
            .returning(MemoryCandidateRow.id)
        )
        created_id = (await self.session.execute(statement)).scalar_one_or_none()
        if created_id is not None:
            row = await self.session.get(MemoryCandidateRow, created_id)
            assert row is not None
            return row, True

        existing = (
            await self.session.execute(
                select(MemoryCandidateRow).where(
                    MemoryCandidateRow.status == "pending",
                    MemoryCandidateRow.fingerprint == fingerprint,
                ).order_by(MemoryCandidateRow.created_at.desc()).limit(1)
            )
        ).scalar_one()
        return existing, False


class MemoryCandidateIntake:
    def __init__(self, factory: async_sessionmaker) -> None:
        self.factory = factory

    async def enqueue_many(
        self,
        raw_candidates: list[dict],
        *,
        source_transcript_id: UUID,
        source_turn_id: UUID,
        source_provider_evidence_id: UUID | None,
    ) -> dict[str, int]:
        accepted = rejected = duplicate = 0
        async with self.factory() as session:
            repository = MemoryCandidateRepository(session)
            for raw in raw_candidates[:8]:
                try:
                    candidate = MemoryCandidate.model_validate(raw)
                    _, created = await repository.enqueue(
                        candidate,
                        source_transcript_id=source_transcript_id,
                        source_turn_id=source_turn_id,
                        source_provider_evidence_id=source_provider_evidence_id,
                    )
                    if created:
                        accepted += 1
                    else:
                        duplicate += 1
                except (TypeError, ValueError):
                    rejected += 1
            await session.commit()
        return {"accepted": accepted, "duplicate": duplicate, "rejected": rejected}
