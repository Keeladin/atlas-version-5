from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas.persistence.models import (
    MemoryCandidateEvidenceRow,
    MemoryCandidateRow,
    TurnRow,
)


class CandidateEvidenceRef(BaseModel):
    """Model-selected evidence handle; canonical IDs are resolved by runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    handle: str | None = Field(default=None, min_length=1, max_length=32)
    turn_id: UUID | None = None
    span_ref: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def validate_reference(self) -> CandidateEvidenceRef:
        if (self.handle is None) == (self.turn_id is None):
            raise ValueError("evidence reference requires exactly one of handle or turn_id")
        return self


def protocol_token(value: str) -> str:
    """Syntax-only normalization: case, surrounding/duplicate whitespace, hyphen and space."""
    return "_".join(value.casefold().replace("-", " ").split())


def normalize_candidate_payload(raw: object) -> object:
    """Fold spelling variants of protocol tokens; never reinterpret meaning.

    ``Project State`` becomes ``project_state`` and ``short-term`` becomes
    ``short_term``. The single vocabulary alias is ``global`` for ``cross_chat``.
    Confidence words, semantic synonyms and unknown kinds are left for validation
    to reject, one candidate at a time.
    """
    if not isinstance(raw, dict):
        return raw
    data = dict(raw)
    for key in ("kind", "scope", "durability"):
        value = data.get(key)
        if isinstance(value, str):
            data[key] = protocol_token(value)
    if data.get("scope") == "global":
        data["scope"] = "cross_chat"
    return data


def candidate_rejection_category(exc: BaseException) -> str:
    """Content-free category for an intake rejection, suitable for aggregation."""
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if not errors:
            return "invalid_candidate"
        error = errors[0]
        location = [str(item) for item in (error.get("loc") or ()) if not isinstance(item, int)]
        field = location[0] if location else "candidate"
        kind = str(error.get("type") or "")
        if kind == "missing":
            return f"missing_{field}"
        if kind == "extra_forbidden":
            return "unexpected_field"
        return f"invalid_{field}"
    if isinstance(exc, ValueError):
        return protocol_token(str(exc))[:64] or "intake_error"
    return "type_error"


class MemoryCandidate(BaseModel):
    """A model-proposed memory item. It is a hint, never memory authority."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _fold_token_spelling(cls, raw: object) -> object:
        return normalize_candidate_payload(raw)

    kind: Literal[
        "identity",
        "preference",
        "fact",
        "decision",
        "relationship",
        "procedure",
        "project_state",
        "intent",
    ]
    content: str = Field(min_length=1, max_length=2_000)
    scope: Literal["chat", "project", "cross_chat"]
    confidence: float = Field(ge=0.0, le=1.0)
    durability: Literal["short_term", "long_term"]
    proposed_action: Literal["upsert"] = "upsert"
    subject: str | None = Field(default=None, max_length=160)
    namespace: str | None = Field(default=None, max_length=160)
    evidence_refs: list[CandidateEvidenceRef] = Field(min_length=1, max_length=8)

    def normalized(self) -> MemoryCandidate:
        data = self.model_dump()
        for key in ("content", "subject", "namespace"):
            value = data.get(key)
            if isinstance(value, str):
                data[key] = " ".join(value.split()).strip() or None
        if not data["content"]:
            raise ValueError("candidate content cannot be empty")
        refs: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in data["evidence_refs"]:
            handle = " ".join(str(item.get("handle") or "").split())[:32] or None
            turn_id = str(item.get("turn_id") or "") or None
            span_ref = " ".join(str(item.get("span_ref") or "").split())[:160]
            key = (handle or "", turn_id or "", span_ref)
            if key in seen:
                continue
            seen.add(key)
            refs.append({"handle": handle, "turn_id": turn_id, "span_ref": span_ref})
        if not refs:
            raise ValueError("candidate requires canonical evidence references")
        data["evidence_refs"] = refs
        return MemoryCandidate.model_validate(data)


def candidate_scope_key(candidate: MemoryCandidate, source_transcript_id: UUID) -> str:
    if candidate.scope == "cross_chat":
        return "owner"
    if candidate.scope == "chat":
        return f"chat:{source_transcript_id}"
    return f"project-unresolved:{source_transcript_id}"


def candidate_fingerprint(candidate: MemoryCandidate, scope_key: str) -> str:
    """Semantic proposal fingerprint. This is not the discovery dedupe key."""
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


def evidence_set_hash(refs: list[tuple[UUID, str]]) -> str:
    material = sorted(f"{turn_id}:{span_ref}" for turn_id, span_ref in refs)
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


def _principal(actor: str) -> str:
    return {
        "owner": "owner",
        "atlas": "assistant",
        "tool": "tool",
    }.get(actor, "other")


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
        allowed_evidence_turn_ids: set[UUID] | None = None,
        evidence_handle_map: dict[str, tuple[UUID, str]] | None = None,
        proposer_model: str | None = None,
        intake_path: str = "foreground",
        origin: str = "conversation",
        temporal_horizon_at=None,
    ) -> tuple[MemoryCandidateRow, bool]:
        candidate = candidate.normalized()
        resolved_refs: list[tuple[UUID, str]] = []
        for ref in candidate.evidence_refs:
            if ref.handle is not None:
                if evidence_handle_map is None or ref.handle not in evidence_handle_map:
                    raise ValueError("candidate referenced an unknown evidence handle")
                turn_id, mapped_span = evidence_handle_map[ref.handle]
                if ref.span_ref and ref.span_ref != mapped_span:
                    raise ValueError("candidate evidence handle/span mismatch")
                resolved_refs.append((turn_id, mapped_span))
            else:
                if evidence_handle_map is not None:
                    raise ValueError("model candidates must use runtime evidence handles")
                assert ref.turn_id is not None
                resolved_refs.append((ref.turn_id, ref.span_ref))
        # Preserve proposer order while removing exact duplicate canonical refs.
        resolved_refs = list(dict.fromkeys(resolved_refs))
        ref_ids = {turn_id for turn_id, _ in resolved_refs}
        if allowed_evidence_turn_ids is not None and not ref_ids.issubset(
            allowed_evidence_turn_ids
        ):
            raise ValueError("candidate referenced evidence outside the model-visible context")

        turns = list(
            (
                await self.session.execute(
                    select(TurnRow).where(TurnRow.id.in_(ref_ids))
                )
            ).scalars()
        )
        by_id = {turn.id: turn for turn in turns}
        if set(by_id) != ref_ids:
            raise ValueError("candidate referenced unknown canonical evidence")
        for turn_id, span_ref in resolved_refs:
            turn = by_id[turn_id]
            if turn.deleted_at is not None:
                raise ValueError("candidate referenced deleted canonical evidence")
            if turn.transcript_id != source_transcript_id:
                raise ValueError("candidate evidence must belong to the source transcript")
            if span_ref:
                try:
                    kind, raw_index = span_ref.split(":", 1)
                    index = int(raw_index)
                except (TypeError, ValueError):
                    raise ValueError("candidate evidence span is malformed") from None
                if kind != "text" or index < 0 or index >= len(turn.blocks or []):
                    raise ValueError("candidate evidence span is unavailable")
                block = (turn.blocks or [])[index]
                if not isinstance(block, dict) or block.get("type") != "text":
                    raise ValueError("candidate evidence span is not canonical text")

        scope_key = candidate_scope_key(candidate, source_transcript_id)
        fingerprint = candidate_fingerprint(candidate, scope_key)
        evidence_hash = evidence_set_hash(resolved_refs)
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
                evidence=None,
                fingerprint=fingerprint,
                evidence_set_hash=evidence_hash,
                proposer_model=(" ".join(str(proposer_model or "").split())[:128] or None),
                intake_path=(" ".join(str(intake_path or "foreground").split())[:32] or "foreground"),
                origin=(" ".join(str(origin or "conversation").split())[:64] or "conversation"),
                temporal_horizon_at=temporal_horizon_at,
                source_transcript_id=source_transcript_id,
                source_turn_id=source_turn_id,
                source_provider_evidence_id=source_provider_evidence_id,
            )
            .on_conflict_do_nothing(
                index_elements=[MemoryCandidateRow.evidence_set_hash],
                index_where=MemoryCandidateRow.status.in_(
                    ["pending", "leased", "retained_short_term", "awaiting_owner"]
                )
                & MemoryCandidateRow.evidence_set_hash.is_not(None),
            )
            .returning(MemoryCandidateRow.id)
        )
        created_id = (await self.session.execute(statement)).scalar_one_or_none()
        if created_id is not None:
            row = await self.session.get(MemoryCandidateRow, created_id)
            assert row is not None
            for ordinal, (turn_id, span_ref) in enumerate(resolved_refs):
                turn = by_id[turn_id]
                self.session.add(
                    MemoryCandidateEvidenceRow(
                        candidate_id=row.id,
                        turn_id=turn.id,
                        principal=_principal(turn.actor),
                        ordinal=ordinal,
                        span_ref=span_ref,
                    )
                )
            await self.session.flush()
            return row, True

        existing = (
            await self.session.execute(
                select(MemoryCandidateRow)
                .where(
                    MemoryCandidateRow.status.in_(
                        ["pending", "leased", "retained_short_term", "awaiting_owner"]
                    ),
                    MemoryCandidateRow.evidence_set_hash == evidence_hash,
                )
                .order_by(MemoryCandidateRow.created_at.desc())
                .limit(1)
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
        allowed_evidence_turn_ids: set[UUID] | None = None,
        evidence_handle_map: dict[str, tuple[UUID, str]] | None = None,
        proposer_model: str | None = None,
        intake_path: str = "foreground",
        origin: str = "conversation",
        temporal_horizon_at=None,
    ) -> dict[str, int]:
        accepted = rejected = duplicate = 0
        rejections: dict[str, int] = {}
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
                        allowed_evidence_turn_ids=allowed_evidence_turn_ids,
                        evidence_handle_map=evidence_handle_map,
                        proposer_model=proposer_model,
                        intake_path=intake_path,
                        origin=origin,
                        temporal_horizon_at=temporal_horizon_at,
                    )
                    if created:
                        accepted += 1
                    else:
                        duplicate += 1
                except (TypeError, ValueError) as exc:
                    rejected += 1
                    category = candidate_rejection_category(exc)
                    rejections[category] = rejections.get(category, 0) + 1
            await session.commit()
        return {
            "accepted": accepted,
            "duplicate": duplicate,
            "rejected": rejected,
            "rejections": rejections,
        }
