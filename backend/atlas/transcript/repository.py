from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import TranscriptRow, TurnRow

from .models import Actor, ContentBlock, Transcript, Turn


class TaskStateConflict(ValueError):
    """The caller's checkpoint snapshot is no longer current."""


_OWNER_CHAT_LOCK = "SELECT pg_advisory_xact_lock(814527, 1)"
_DEFAULT_CHAT_TITLE = "New chat"


def _clean_title(value: str | None, *, default: str = _DEFAULT_CHAT_TITLE) -> str:
    title = " ".join((value or "").split()).strip()
    if not title:
        title = default
    return title[:120]


def _suggest_title(blocks: list[ContentBlock]) -> str | None:
    for block in blocks:
        if getattr(block, "type", None) != "text":
            continue
        text_value = " ".join(str(getattr(block, "text", "")).split()).strip()
        if not text_value:
            continue
        if len(text_value) <= 64:
            return text_value
        return text_value[:61].rstrip() + "…"
    return None


def _to_transcript(row: TranscriptRow) -> Transcript:
    created_at = row.created_at or datetime.now(UTC)
    return Transcript(
        id=row.id,
        kind=row.kind,
        created_at=created_at,
        updated_at=row.updated_at or created_at,
        title=row.title,
        closed_at=row.closed_at,
        context_summary=row.context_summary,
        summarized_through_turn_id=row.summarized_through_turn_id,
        active_task_state=row.active_task_state or {},
    )


class TranscriptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, kind: str = "owner", title: str | None = None) -> Transcript:
        row = TranscriptRow(
            kind=kind,
            title=_clean_title(title) if kind == "owner" else title,
        )
        self.session.add(row)
        await self.session.flush()
        return _to_transcript(row)

    async def get_or_create_active(self) -> Transcript:
        query = (
            select(TranscriptRow)
            .where(TranscriptRow.closed_at.is_(None), TranscriptRow.kind == "owner")
            .order_by(TranscriptRow.created_at.desc(), TranscriptRow.id.desc())
            .limit(1)
        )
        row = (await self.session.execute(query)).scalar_one_or_none()
        if row is None:
            await self.session.execute(text(_OWNER_CHAT_LOCK))
            row = (await self.session.execute(query)).scalar_one_or_none()
            if row is None:
                return await self.create()
        return _to_transcript(row)

    async def get_owner_chat(self, chat_id: UUID) -> Transcript:
        row = (await self.session.execute(
            select(TranscriptRow).where(
                TranscriptRow.id == chat_id,
                TranscriptRow.kind == "owner",
            )
        )).scalar_one_or_none()
        if row is None:
            raise LookupError("Chat not found")
        return _to_transcript(row)

    async def list_owner_chats(self) -> list[Transcript]:
        rows = list((await self.session.execute(
            select(TranscriptRow)
            .where(TranscriptRow.kind == "owner")
            .order_by(TranscriptRow.updated_at.desc(), TranscriptRow.created_at.desc(), TranscriptRow.id.desc())
        )).scalars())
        return [_to_transcript(row) for row in rows]

    async def create_owner_chat(self, *, title: str | None = None) -> Transcript:
        await self.session.execute(text(_OWNER_CHAT_LOCK))
        await self.session.execute(
            update(TranscriptRow)
            .where(TranscriptRow.kind == "owner", TranscriptRow.closed_at.is_(None))
            .values(closed_at=func.now())
        )
        return await self.create(kind="owner", title=title)

    async def activate_owner_chat(self, chat_id: UUID) -> Transcript:
        await self.session.execute(text(_OWNER_CHAT_LOCK))
        row = (await self.session.execute(
            select(TranscriptRow)
            .where(TranscriptRow.id == chat_id, TranscriptRow.kind == "owner")
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise LookupError("Chat not found")
        if row.closed_at is not None:
            await self.session.execute(
                update(TranscriptRow)
                .where(
                    TranscriptRow.kind == "owner",
                    TranscriptRow.closed_at.is_(None),
                    TranscriptRow.id != chat_id,
                )
                .values(closed_at=func.now())
            )
            row.closed_at = None
            await self.session.flush()
        return _to_transcript(row)

    async def rename_owner_chat(self, chat_id: UUID, title: str) -> Transcript:
        clean = _clean_title(title, default="")
        if not clean:
            raise ValueError("Chat title cannot be empty")
        row = (await self.session.execute(
            select(TranscriptRow).where(
                TranscriptRow.id == chat_id,
                TranscriptRow.kind == "owner",
            ).with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise LookupError("Chat not found")
        row.title = clean
        row.updated_at = datetime.now(UTC)
        await self.session.flush()
        return _to_transcript(row)

    async def delete_owner_chat(self, chat_id: UUID) -> Transcript:
        await self.session.execute(text(_OWNER_CHAT_LOCK))
        row = (await self.session.execute(
            select(TranscriptRow).where(
                TranscriptRow.id == chat_id,
                TranscriptRow.kind == "owner",
            ).with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise LookupError("Chat not found")
        was_active = row.closed_at is None
        await self.session.delete(row)
        await self.session.flush()

        if was_active:
            replacement = (await self.session.execute(
                select(TranscriptRow)
                .where(TranscriptRow.kind == "owner")
                .order_by(TranscriptRow.updated_at.desc(), TranscriptRow.created_at.desc(), TranscriptRow.id.desc())
                .limit(1)
                .with_for_update()
            )).scalar_one_or_none()
            if replacement is None:
                return await self.create(kind="owner")
            replacement.closed_at = None
            await self.session.flush()
            return _to_transcript(replacement)
        return await self.get_or_create_active()

    async def get_active_task_state(self, transcript_id: UUID) -> dict:
        row = await self.session.get(TranscriptRow, transcript_id)
        if row is None:
            raise LookupError("Transcript not found")
        return dict(row.active_task_state or {})

    async def update_active_task_state(self, transcript_id: UUID, state: dict, *, expected_revision: int) -> dict:
        state = deepcopy(state)
        state.setdefault("task_id", str(uuid4()))
        state["revision"] = expected_revision + 1
        result = await self.session.execute(
            update(TranscriptRow)
            .where(TranscriptRow.id == transcript_id, TranscriptRow.active_task_revision == expected_revision)
            .values(active_task_state=state, active_task_revision=expected_revision + 1)
        )
        if result.rowcount != 1:
            raise TaskStateConflict("Active task changed; reload before updating")
        return state

    async def mutate_active_task_state(
        self, transcript_id: UUID, transform: Callable[[dict], dict], *, expected_task_id: str | None = None
    ) -> dict:
        row = (await self.session.execute(
            select(TranscriptRow).where(TranscriptRow.id == transcript_id)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if row is None:
            raise LookupError("Transcript not found")
        current = deepcopy(row.active_task_state or {})
        if expected_task_id is not None and current.get("task_id") != expected_task_id:
            raise TaskStateConflict("The response belongs to a different active task")
        updated = transform(current)
        if updated == current:
            return current
        return await self.update_active_task_state(
            transcript_id, updated, expected_revision=row.active_task_revision
        )

    async def update_context_summary(self, transcript_id: UUID, *, summary: str, summarized_through_turn_id: UUID) -> None:
        row = await self.session.get(TranscriptRow, transcript_id)
        if row is None:
            raise LookupError("Transcript not found")
        row.context_summary = summary
        row.summarized_through_turn_id = summarized_through_turn_id
        await self.session.flush()

    async def append_turn(
        self,
        transcript_id: UUID,
        actor: Actor,
        blocks: list[ContentBlock],
    ) -> Turn:
        sequence = (await self.session.execute(
            update(TranscriptRow).where(TranscriptRow.id == transcript_id)
            .values(
                next_turn_sequence=TranscriptRow.next_turn_sequence + 1,
                updated_at=func.now(),
            )
            .returning(TranscriptRow.next_turn_sequence)
        )).scalar_one()
        row = TurnRow(
            transcript_id=transcript_id,
            sequence=sequence,
            actor=actor.value,
            blocks=[block.model_dump(mode="json") for block in blocks],
        )
        self.session.add(row)
        if actor == Actor.OWNER and sequence == 1:
            suggested = _suggest_title(blocks)
            if suggested:
                await self.session.execute(
                    update(TranscriptRow)
                    .where(
                        TranscriptRow.id == transcript_id,
                        TranscriptRow.kind == "owner",
                        TranscriptRow.title == _DEFAULT_CHAT_TITLE,
                    )
                    .values(title=suggested)
                )
        await self.session.flush()
        return Turn(
            id=row.id,
            transcript_id=row.transcript_id,
            sequence=row.sequence,
            actor=Actor(row.actor),
            blocks=row.blocks,
            created_at=row.created_at,
        )

    async def list_recent_turns(self, transcript_id: UUID, *, exchanges: int = 20, limit: int = 500) -> list[Turn]:
        owner_sequences = list((await self.session.execute(select(TurnRow.sequence).where(
            TurnRow.transcript_id == transcript_id, TurnRow.actor == Actor.OWNER.value)
            .order_by(TurnRow.sequence.desc()).limit(max(1, min(exchanges, 50))))).scalars())
        turns = await self.list_turns(transcript_id, limit=limit,
            after_sequence=min(owner_sequences) - 1 if owner_sequences else None)
        if owner_sequences and not any(turn.sequence == owner_sequences[0] for turn in turns):
            owner = await self.list_turns(transcript_id, limit=1, before_sequence=owner_sequences[0] + 1)
            turns = owner + turns[-(limit - 1):]
        return turns

    async def list_turns(self, transcript_id: UUID, *, limit: int = 200,
            before_sequence: int | None = None, after_sequence: int | None = None) -> list[Turn]:
        result = await self.session.execute(
            select(TurnRow)
            .where(TurnRow.transcript_id == transcript_id)
            .where(TurnRow.sequence < before_sequence if before_sequence is not None else True,
                TurnRow.sequence > after_sequence if after_sequence is not None else True)
            .order_by(TurnRow.sequence.desc()).limit(max(1, min(limit, 500)))
        )
        return list(reversed([
            Turn(
                id=row.id,
                transcript_id=row.transcript_id,
                sequence=row.sequence,
                actor=Actor(row.actor),
                blocks=row.blocks,
                created_at=row.created_at,
            )
            for row in result.scalars()
        ]))
