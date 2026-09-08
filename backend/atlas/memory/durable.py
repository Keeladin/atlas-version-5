from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCommandRow,
    TranscriptRow,
    TurnRow,
)

ACTIVE = "active"
SUPERSEDED = "superseded"
FORGOTTEN = "forgotten"
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

    async def latest_owner_source(self) -> tuple[UUID | None, UUID | None]:
        statement = (
            select(TranscriptRow.id, TurnRow.id)
            .join(TurnRow, TurnRow.transcript_id == TranscriptRow.id)
            .where(
                TranscriptRow.kind == "owner",
                TranscriptRow.closed_at.is_(None),
                TurnRow.actor == "owner",
            )
            .order_by(TurnRow.sequence.desc())
            .limit(1)
        )
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

    async def clear_guards(self, fingerprint: str) -> None:
        await self.session.execute(
            update(DurableMemoryRow)
            .where(
                DurableMemoryRow.fingerprint == fingerprint,
                DurableMemoryRow.suppresses_recall.is_(True),
            )
            .values(suppresses_recall=False, updated_at=utcnow())
        )

    async def create_active(
        self,
        content: str,
        *,
        source_transcript_id: UUID | None,
        source_turn_id: UUID | None,
        supersedes_id: UUID | None = None,
    ) -> tuple[DurableMemoryRow, bool]:
        fingerprint = memory_fingerprint(content)
        await self.clear_guards(fingerprint)
        existing = await self.active_by_fingerprint(fingerprint)
        if existing is not None:
            return existing, False
        row = DurableMemoryRow(
            status=ACTIVE,
            record_kind="owner_directed",
            content=content,
            fingerprint=fingerprint,
            suppresses_recall=False,
            source_transcript_id=source_transcript_id,
            source_turn_id=source_turn_id,
            supersedes_id=supersedes_id,
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
        fingerprint = memory_fingerprint(content)
        existing = (
            await self.session.execute(
                select(DurableMemoryRow).where(
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
            content=content,
            fingerprint=fingerprint,
            suppresses_recall=True,
            source_transcript_id=source_transcript_id,
            source_turn_id=source_turn_id,
            forgotten_at=utcnow() if status == FORGOTTEN else None,
        )
        self.session.add(row)
        await self.session.flush()
        return row, True

    async def search_active(
        self,
        query: str,
        *,
        limit: int = 5,
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
            DurableMemoryRow.search_vector.op("@@")(tsquery),
        )
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
                DurableMemoryRow.embedding.is_not(None),
                DurableMemoryRow.embedding_model == embedding_model,
            )
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
            "forgotten_memories": memories.get(FORGOTTEN, 0),
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
        return {
            "memory_id": str(row.id),
            "source_class": "owner_canonical_memory",
            "status": row.status,
            "record_kind": row.record_kind,
            "content": row.content,
            "source_transcript_id": (
                str(row.source_transcript_id) if row.source_transcript_id else None
            ),
            "source_turn_id": str(row.source_turn_id) if row.source_turn_id else None,
            "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "lexical_rank": None,
            "semantic_similarity": None,
            "hybrid_rank": None,
            "retrieval_sources": [],
            "authority": "owner_directed",
        }


class DurableMemoryCommands:
    def __init__(self, factory: async_sessionmaker) -> None:
        self.factory = factory

    async def remember(self, arguments: dict) -> dict[str, object]:
        content = clean_memory_content(arguments.get("content"))
        command_id, source = await self._begin("remember", {"content": content})
        try:
            async with self.factory() as session:
                repository = DurableMemoryRepository(session)
                memory, created = await repository.create_active(
                    content,
                    source_transcript_id=source[0],
                    source_turn_id=source[1],
                )
                command = await session.get(MemoryCommandRow, command_id)
                assert command is not None
                command.status = APPLIED
                command.replacement_memory_id = memory.id
                command.applied_at = utcnow()
                await session.commit()
                return {
                    "command_id": str(command_id),
                    "status": APPLIED,
                    "operation": "remember",
                    "result": "created" if created else "already_active",
                    "memory": DurableMemoryRepository.project(memory),
                }
        except Exception as exc:
            await self._fail(command_id, exc)
            raise MemoryMutationError(
                f"remember failed (command_id={command_id}): {exc}"
            ) from exc

    async def correct(self, arguments: dict) -> dict[str, object]:
        new_content = clean_memory_content(arguments.get("content"))
        memory_raw = arguments.get("memory_id")
        old_raw = arguments.get("old_content")
        old_content = clean_memory_content(old_raw, "old_content") if old_raw is not None else None
        if memory_raw is None and old_content is None:
            raise ValueError("correct requires memory_id or old_content")
        command_arguments = {"content": new_content}
        if memory_raw is not None:
            command_arguments["memory_id"] = str(memory_raw)
        if old_content is not None:
            command_arguments["old_content"] = old_content
        command_id, source = await self._begin("correct", command_arguments)
        try:
            async with self.factory() as session:
                repository = DurableMemoryRepository(session)
                target = None
                if memory_raw is not None:
                    target = await repository.get(UUID(str(memory_raw)))
                    if target is None:
                        raise ValueError("target memory does not exist")
                    if target.status != ACTIVE:
                        raise ValueError("target memory is not active")
                    if old_content is not None and target.fingerprint != memory_fingerprint(old_content):
                        raise ValueError("old_content does not match target memory")
                elif old_content is not None:
                    target = await repository.active_by_fingerprint(memory_fingerprint(old_content))
                    if target is None:
                        target, _ = await repository.create_guard(
                            old_content,
                            status=SUPERSEDED,
                            record_kind="owner_correction_guard",
                            source_transcript_id=source[0],
                            source_turn_id=source[1],
                        )

                assert target is not None
                new_fingerprint = memory_fingerprint(new_content)
                if target.status == ACTIVE and target.fingerprint == new_fingerprint:
                    replacement = target
                    created = False
                    await repository.clear_guards(new_fingerprint)
                else:
                    replacement, created = await repository.create_active(
                        new_content,
                        source_transcript_id=source[0],
                        source_turn_id=source[1],
                        supersedes_id=target.id,
                    )
                    if target.id != replacement.id:
                        target.status = SUPERSEDED
                        target.suppresses_recall = True
                        target.superseded_by_id = replacement.id
                        target.embedding = None
                        target.embedding_model = None
                        target.embedding_dimensions = None
                        target.embedded_at = None
                        target.updated_at = utcnow()

                command = await session.get(MemoryCommandRow, command_id)
                assert command is not None
                command.status = APPLIED
                command.target_memory_id = target.id
                command.replacement_memory_id = replacement.id
                command.applied_at = utcnow()
                await session.commit()
                suppression = [target.content] if target.id != replacement.id else []
                return {
                    "command_id": str(command_id),
                    "status": APPLIED,
                    "operation": "correct",
                    "result": "created" if created else "already_current",
                    "target_memory_id": str(target.id),
                    "memory": DurableMemoryRepository.project(replacement),
                    "_context_suppression": {"contents": suppression},
                }
        except Exception as exc:
            await self._fail(command_id, exc)
            raise MemoryMutationError(
                f"correct failed (command_id={command_id}): {exc}"
            ) from exc

    async def forget(self, arguments: dict) -> dict[str, object]:
        memory_raw = arguments.get("memory_id")
        content_raw = arguments.get("content")
        content = clean_memory_content(content_raw) if content_raw is not None else None
        if memory_raw is None and content is None:
            raise ValueError("forget requires memory_id or content")
        command_arguments: dict[str, object] = {}
        if memory_raw is not None:
            command_arguments["memory_id"] = str(memory_raw)
        if content is not None:
            command_arguments["content"] = content
        command_id, source = await self._begin("forget", command_arguments)
        try:
            async with self.factory() as session:
                repository = DurableMemoryRepository(session)
                target = None
                if memory_raw is not None:
                    target = await repository.get(UUID(str(memory_raw)))
                    if target is None:
                        raise ValueError("target memory does not exist")
                    if target.status != ACTIVE:
                        raise ValueError("target memory is not active")
                    if content is not None and target.fingerprint != memory_fingerprint(content):
                        raise ValueError("content does not match target memory")
                if target is None and content is not None:
                    target = await repository.active_by_fingerprint(memory_fingerprint(content))

                if target is None:
                    assert content is not None
                    target, guard_created = await repository.create_guard(
                        content,
                        status=FORGOTTEN,
                        record_kind="owner_forget_tombstone",
                        source_transcript_id=source[0],
                        source_turn_id=source[1],
                    )
                    result = "tombstone_created" if guard_created else "already_forgotten"
                else:
                    target.status = FORGOTTEN
                    target.suppresses_recall = True
                    target.embedding = None
                    target.embedding_model = None
                    target.embedding_dimensions = None
                    target.embedded_at = None
                    target.forgotten_at = utcnow()
                    target.updated_at = utcnow()
                    result = "forgotten"

                command = await session.get(MemoryCommandRow, command_id)
                assert command is not None
                command.status = APPLIED
                command.target_memory_id = target.id
                command.applied_at = utcnow()
                await session.commit()
                return {
                    "command_id": str(command_id),
                    "status": APPLIED,
                    "operation": "forget",
                    "result": result,
                    "memory_id": str(target.id),
                    "_context_suppression": {"contents": [target.content]},
                }
        except Exception as exc:
            await self._fail(command_id, exc)
            raise MemoryMutationError(
                f"forget failed (command_id={command_id}): {exc}"
            ) from exc

    async def commands(self, arguments: dict) -> dict[str, object]:
        status = str(arguments.get("status") or "").strip() or None
        limit = int(arguments.get("limit") or 20)
        async with self.factory() as session:
            repository = DurableMemoryRepository(session)
            return {
                "commands": await repository.list_commands(limit=limit, status=status),
                "state": await repository.state(),
            }

    async def _begin(
        self, operation: str, arguments: dict[str, object]
    ) -> tuple[UUID, tuple[UUID | None, UUID | None]]:
        async with self.factory() as session:
            repository = DurableMemoryRepository(session)
            source = await repository.latest_owner_source()
            command = MemoryCommandRow(
                operation=operation,
                status=PENDING,
                arguments_json=arguments,
                source_transcript_id=source[0],
                source_turn_id=source[1],
            )
            session.add(command)
            await session.commit()
            return command.id, source

    async def _fail(self, command_id: UUID, exc: Exception) -> None:
        async with self.factory() as session:
            command = await session.get(MemoryCommandRow, command_id)
            if command is None:
                return
            command.status = FAILED
            command.error = f"{type(exc).__name__}: {exc}"[:2_000]
            command.failed_at = utcnow()
            await session.commit()
