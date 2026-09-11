from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import (
    ContinuityCapsuleRow,
    DurableMemoryRow,
    MemoryCommandRow,
    TranscriptRow,
    TurnRow,
)

ACTIVE = "active"
SUPERSEDED = "superseded"
RETIRED = "retired"
DELETED = "deleted"
VERIFIED = "verified"
LEGACY_UNVERIFIED = "legacy_unverified"
PENDING = "pending"
APPLIED = "applied"
FAILED = "failed"
_RRF_K = 60
_MAX_CONTENT_CHARS = 4_000


class MemoryMutationError(RuntimeError):
    pass


def clean_memory_content(value: object, field: str = "content") -> str:
    content = " ".join(str(value or "").split())
    if not content:
        raise ValueError(f"{field} cannot be empty")
    if len(content) > _MAX_CONTENT_CHARS:
        raise ValueError(f"{field} exceeds {_MAX_CONTENT_CHARS} characters")
    return content


def memory_fingerprint(content: str) -> str:
    normalized = " ".join(content.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)


class DurableMemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def latest_owner_source(
        self, transcript_id: UUID | None = None
    ) -> tuple[UUID | None, UUID | None]:
        statement = (
            select(TranscriptRow.id, TurnRow.id)
            .join(TurnRow, TurnRow.transcript_id == TranscriptRow.id)
            .where(TranscriptRow.kind == "owner", TurnRow.actor == "owner")
        )
        if transcript_id is not None:
            statement = statement.where(TranscriptRow.id == transcript_id)
        else:
            statement = statement.where(TranscriptRow.closed_at.is_(None))
        statement = statement.order_by(TurnRow.sequence.desc()).limit(1)
        row = (await self.session.execute(statement)).first()
        return (row[0], row[1]) if row is not None else (None, None)

    async def get(self, memory_id: UUID) -> DurableMemoryRow | None:
        return await self.session.get(DurableMemoryRow, memory_id)

    async def active_by_fingerprint(self, fingerprint: str) -> DurableMemoryRow | None:
        statement = select(DurableMemoryRow).where(
            DurableMemoryRow.status == ACTIVE,
            DurableMemoryRow.fingerprint == fingerprint,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def retired_by_fingerprint(self, fingerprint: str) -> DurableMemoryRow | None:
        statement = (
            select(DurableMemoryRow)
            .where(
                DurableMemoryRow.status == RETIRED,
                DurableMemoryRow.fingerprint == fingerprint,
            )
            .order_by(DurableMemoryRow.updated_at.desc(), DurableMemoryRow.id.desc())
            .limit(1)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def clear_guards(self, fingerprint: str) -> bool:
        result = await self.session.execute(
            update(DurableMemoryRow)
            .where(
                DurableMemoryRow.fingerprint == fingerprint,
                DurableMemoryRow.suppresses_recall.is_(True),
                DurableMemoryRow.status != DELETED,
            )
            .values(suppresses_recall=False, updated_at=utcnow())
        )
        return bool(result.rowcount)

    async def invalidate_continuity_capsules(self) -> None:
        await self.session.execute(delete(ContinuityCapsuleRow))

    async def create_active(
        self,
        content: str,
        *,
        source_transcript_id: UUID | None,
        source_turn_id: UUID | None,
        supersedes_id: UUID | None = None,
        record_kind: str = "owner_directed",
        origin: str = "runtime_unclassified",
        memory_kind: str | None = None,
        scope: str = "cross_chat",
        scope_key: str | None = None,
        durability: str = "long_term",
        subject: str | None = None,
        namespace: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        grounding_status: str = "legacy_unverified",
    ) -> tuple[DurableMemoryRow, bool]:
        fingerprint = memory_fingerprint(content)
        existing = await self.active_by_fingerprint(fingerprint)
        if existing is not None:
            return existing, False
        row = DurableMemoryRow(
            status=ACTIVE,
            record_kind=record_kind,
            origin=origin,
            grounding_status=grounding_status,
            memory_kind=memory_kind,
            scope=scope,
            scope_key=scope_key,
            durability=durability,
            subject=subject,
            namespace=namespace,
            content=content,
            fingerprint=fingerprint,
            suppresses_recall=False,
            source_transcript_id=source_transcript_id,
            source_turn_id=source_turn_id,
            supersedes_id=supersedes_id,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self.session.add(row)
        await self.session.flush()
        return row, True

    async def create_guard(
        self,
        content: str,
        *,
        status: str,
        record_kind: str,
        source_transcript_id: UUID | None,
        source_turn_id: UUID | None,
    ) -> tuple[DurableMemoryRow, bool]:
        if status not in (SUPERSEDED, RETIRED):
            raise ValueError("guards may only be superseded or retired")
        fingerprint = memory_fingerprint(content)
        existing = (
            await self.session.execute(
                select(DurableMemoryRow)
                .where(
                    DurableMemoryRow.fingerprint == fingerprint,
                    DurableMemoryRow.status == status,
                    DurableMemoryRow.suppresses_recall.is_(True),
                )
                .order_by(DurableMemoryRow.created_at.desc(), DurableMemoryRow.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        row = DurableMemoryRow(
            status=status,
            record_kind=record_kind,
            origin="owner_lifecycle",
            grounding_status="verified",
            scope="cross_chat",
            durability="long_term",
            content=content,
            fingerprint=fingerprint,
            suppresses_recall=True,
            source_transcript_id=source_transcript_id,
            source_turn_id=source_turn_id,
            retired_at=utcnow() if status == RETIRED else None,
        )
        self.session.add(row)
        await self.session.flush()
        return row, True

    @staticmethod
    def _applicable(statement, transcript_id: UUID | None):
        if transcript_id is None:
            return statement.where(DurableMemoryRow.scope == "cross_chat")
        chat_key = f"chat:{transcript_id}"
        return statement.where(
            or_(
                DurableMemoryRow.scope == "cross_chat",
                (DurableMemoryRow.scope == "chat")
                & (DurableMemoryRow.scope_key == chat_key),
            )
        )

    async def search_active(
        self,
        query: str,
        *,
        limit: int = 5,
        transcript_id: UUID | None = None,
        query_embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> list[dict[str, object]]:
        normalized = query.strip()
        if not normalized:
            return []
        bounded_limit = max(1, min(limit, 10))
        candidate_limit = max(20, min(40, bounded_limit * 4))
        tsquery = func.websearch_to_tsquery("simple", normalized)
        lexical_rank = func.ts_rank_cd(
            DurableMemoryRow.search_vector, tsquery
        ).label("lexical_rank")
        lexical = select(DurableMemoryRow, lexical_rank).where(
            DurableMemoryRow.status == ACTIVE,
            DurableMemoryRow.grounding_status == VERIFIED,
            DurableMemoryRow.search_vector.op("@@")(tsquery),
        )
        lexical = self._applicable(lexical, transcript_id)
        lexical_rows = (
            await self.session.execute(
                lexical.order_by(lexical_rank.desc(), DurableMemoryRow.updated_at.desc()).limit(
                    candidate_limit
                )
            )
        ).all()

        semantic_rows = []
        if query_embedding is not None and embedding_model:
            distance = DurableMemoryRow.embedding.cosine_distance(query_embedding).label(
                "semantic_distance"
            )
            semantic = select(DurableMemoryRow, distance).where(
                DurableMemoryRow.status == ACTIVE,
                DurableMemoryRow.grounding_status == VERIFIED,
                DurableMemoryRow.embedding.is_not(None),
                DurableMemoryRow.embedding_model == embedding_model,
            )
            semantic = self._applicable(semantic, transcript_id)
            semantic_rows = (
                await self.session.execute(
                    semantic.order_by(distance.asc(), DurableMemoryRow.updated_at.desc()).limit(
                        candidate_limit
                    )
                )
            ).all()

        combined: dict[str, dict[str, object]] = {}
        scores: dict[str, float] = {}
        for position, (row, score) in enumerate(lexical_rows, start=1):
            key = str(row.id)
            item = combined.setdefault(key, self.project(row))
            item["lexical_rank"] = float(score or 0.0)
            sources = item["retrieval_sources"]
            assert isinstance(sources, list)
            sources.append("lexical")
            scores[key] = scores.get(key, 0.0) + 1.1 / (_RRF_K + position)

        for position, (row, distance_value) in enumerate(semantic_rows, start=1):
            key = str(row.id)
            item = combined.setdefault(key, self.project(row))
            distance_float = float(distance_value if distance_value is not None else 2.0)
            item["semantic_similarity"] = 1.0 - distance_float
            sources = item["retrieval_sources"]
            assert isinstance(sources, list)
            sources.append("semantic")
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + position)

        for key, item in combined.items():
            item["hybrid_rank"] = scores.get(key, 0.0)
        ordered = sorted(
            combined.values(),
            key=lambda item: (
                float(item.get("hybrid_rank") or 0.0),
                str(item.get("updated_at") or ""),
            ),
            reverse=True,
        )
        return ordered[:bounded_limit]

    async def search_historical(
        self,
        query: str,
        *,
        limit: int = 5,
        transcript_id: UUID | None = None,
    ) -> list[dict[str, object]]:
        normalized = query.strip()
        if not normalized:
            return []
        bounded_limit = max(1, min(limit, 10))
        tsquery = func.websearch_to_tsquery("simple", normalized)
        lexical_rank = func.ts_rank_cd(
            DurableMemoryRow.search_vector, tsquery
        ).label("lexical_rank")
        statement = select(DurableMemoryRow, lexical_rank).where(
            DurableMemoryRow.status == SUPERSEDED,
            DurableMemoryRow.grounding_status == VERIFIED,
            DurableMemoryRow.search_vector.op("@@")(tsquery),
        )
        statement = self._applicable(statement, transcript_id)
        rows = (await self.session.execute(
            statement.order_by(
                lexical_rank.desc(), DurableMemoryRow.updated_at.desc()
            ).limit(bounded_limit)
        )).all()
        results: list[dict[str, object]] = []
        for row, score in rows:
            item = self.project(row)
            item["lexical_rank"] = float(score or 0.0)
            item["hybrid_rank"] = float(score or 0.0)
            item["retrieval_sources"] = ["historical_lexical"]
            results.append(item)
        return results

    async def recall_guards(self) -> list[dict[str, object]]:
        statement = select(DurableMemoryRow).where(
            DurableMemoryRow.suppresses_recall.is_(True)
        ).order_by(DurableMemoryRow.created_at.asc(), DurableMemoryRow.id.asc())
        rows = list((await self.session.execute(statement)).scalars())
        return [
            {
                "memory_id": str(row.id),
                "content": row.content,
                "fingerprint": row.fingerprint,
                "source_turn_id": str(row.source_turn_id) if row.source_turn_id else None,
                "status": row.status,
            }
            for row in rows
        ]

    async def state(self) -> dict[str, object]:
        memory_rows = (
            await self.session.execute(
                select(DurableMemoryRow.status, func.count(DurableMemoryRow.id)).group_by(
                    DurableMemoryRow.status
                )
            )
        ).all()
        command_rows = (
            await self.session.execute(
                select(MemoryCommandRow.status, func.count(MemoryCommandRow.id)).group_by(
                    MemoryCommandRow.status
                )
            )
        ).all()
        memories = {str(status): int(count or 0) for status, count in memory_rows}
        commands = {str(status): int(count or 0) for status, count in command_rows}
        return {
            "active_memories": memories.get(ACTIVE, 0),
            "superseded_memories": memories.get(SUPERSEDED, 0),
            "retired_memories": memories.get(RETIRED, 0),
            "deleted_memory_identities": memories.get(DELETED, 0),
            "legacy_unverified_memories": int(
                (
                    await self.session.execute(
                        select(func.count(DurableMemoryRow.id)).where(
                            DurableMemoryRow.grounding_status == LEGACY_UNVERIFIED
                        )
                    )
                ).scalar_one()
                or 0
            ),
            "suppression_guards": int(
                (
                    await self.session.execute(
                        select(func.count(DurableMemoryRow.id)).where(
                            DurableMemoryRow.suppresses_recall.is_(True)
                        )
                    )
                ).scalar_one()
                or 0
            ),
            "pending_commands": commands.get(PENDING, 0),
            "applied_commands": commands.get(APPLIED, 0),
            "failed_commands": commands.get(FAILED, 0),
        }

    async def list_commands(
        self, *, limit: int = 20, status: str | None = None
    ) -> list[dict[str, object]]:
        bounded = max(1, min(limit, 100))
        statement = select(MemoryCommandRow).order_by(
            MemoryCommandRow.created_at.desc(), MemoryCommandRow.id.desc()
        )
        if status:
            statement = statement.where(MemoryCommandRow.status == status)
        rows = list((await self.session.execute(statement.limit(bounded))).scalars())
        return [
            {
                "command_id": str(row.id),
                "operation": row.operation,
                "status": row.status,
                "target_memory_id": str(row.target_memory_id) if row.target_memory_id else None,
                "replacement_memory_id": (
                    str(row.replacement_memory_id) if row.replacement_memory_id else None
                ),
                "source_transcript_id": (
                    str(row.source_transcript_id) if row.source_transcript_id else None
                ),
                "source_turn_id": str(row.source_turn_id) if row.source_turn_id else None,
                "error": row.error,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "applied_at": row.applied_at.isoformat() if row.applied_at else None,
                "failed_at": row.failed_at.isoformat() if row.failed_at else None,
            }
            for row in rows
        ]

    @staticmethod
    def project(row: DurableMemoryRow) -> dict[str, object]:
        owner_directed = row.record_kind.startswith("owner_") or row.record_kind == "owner_directed"
        return {
            "memory_id": str(row.id),
            "source_class": "owner_canonical_memory" if owner_directed else "derived_memory",
            "status": row.status,
            "record_kind": row.record_kind,
            "grounding_status": row.grounding_status,
            "memory_kind": row.memory_kind,
            "scope": row.scope,
            "scope_key": row.scope_key,
            "durability": row.durability,
            "subject": row.subject,
            "namespace": row.namespace,
            "content": row.content,
            "source_transcript_id": (
                str(row.source_transcript_id) if row.source_transcript_id else None
            ),
            "source_turn_id": str(row.source_turn_id) if row.source_turn_id else None,
            "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
            "superseded_by_id": str(row.superseded_by_id) if row.superseded_by_id else None,
            "valid_from": row.valid_from.isoformat() if row.valid_from else None,
            "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "retired_at": row.retired_at.isoformat() if row.retired_at else None,
            "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "lexical_rank": None,
            "semantic_similarity": None,
            "hybrid_rank": None,
            "retrieval_sources": [],
            "authority": "owner_directed" if owner_directed else "derived",
        }
