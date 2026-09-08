from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import (
    TranscriptIndexChunkRow,
    TranscriptIndexStateRow,
    TranscriptRow,
    TurnRow,
)

INDEX_VERSION = "text-v1"


@dataclass(frozen=True)
class IndexRunResult:
    transcripts_seen: int = 0
    transcripts_advanced: int = 0
    chunks_created: int = 0
    turns_processed: int = 0


def _turn_text(row: TurnRow) -> str:
    parts: list[str] = []
    for block in row.blocks or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
        elif block_type == "tool_observation":
            operation = str(block.get("operation") or "runtime")
            phase = str(block.get("phase") or "observed")
            summary = str(block.get("summary") or "").strip()
            label = f"{operation} [{phase}]"
            parts.append(f"{label}: {summary}" if summary else label)
    if not parts:
        return ""
    return f"{row.actor}: " + "\n".join(parts)


def _chunk_rows(rows: list[TurnRow], max_chars: int) -> list[tuple[list[TurnRow], str]]:
    chunks: list[tuple[list[TurnRow], str]] = []
    current_rows: list[TurnRow] = []
    current_parts: list[str] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current_rows, current_parts, current_chars
        if current_rows and current_parts:
            chunks.append((current_rows, "\n\n".join(current_parts)))
        current_rows = []
        current_parts = []
        current_chars = 0

    for row in rows:
        text = _turn_text(row)
        if not text:
            continue
        addition = len(text) + (2 if current_parts else 0)
        if current_rows and current_chars + addition > max_chars:
            flush()
        current_rows.append(row)
        current_parts.append(text)
        current_chars += addition
    flush()
    return chunks


class TranscriptIndexer:
    """Deterministically derives searchable chunks from canonical transcript turns."""

    def __init__(self, session: AsyncSession, *, max_chars: int = 4_000) -> None:
        self.session = session
        self.max_chars = max(500, max_chars)

    async def _state_for_update(self, transcript_id: UUID) -> TranscriptIndexStateRow:
        await self.session.execute(
            insert(TranscriptIndexStateRow)
            .values(transcript_id=transcript_id, index_version=INDEX_VERSION)
            .on_conflict_do_nothing(index_elements=["transcript_id", "index_version"])
        )
        state = (
            await self.session.execute(
                select(TranscriptIndexStateRow)
                .where(
                    TranscriptIndexStateRow.transcript_id == transcript_id,
                    TranscriptIndexStateRow.index_version == INDEX_VERSION,
                )
                .with_for_update()
            )
        ).scalar_one()
        return state

    async def index_transcript(self, transcript_id: UUID, *, through_sequence: int) -> tuple[int, int]:
        if through_sequence <= 0:
            return 0, 0
        state = await self._state_for_update(transcript_id)
        if through_sequence <= state.last_indexed_sequence:
            return 0, 0
        rows = list((await self.session.execute(
            select(TurnRow)
            .where(
                TurnRow.transcript_id == transcript_id,
                TurnRow.sequence > state.last_indexed_sequence,
                TurnRow.sequence <= through_sequence,
            )
            .order_by(TurnRow.sequence)
        )).scalars())
        if not rows:
            state.last_indexed_sequence = through_sequence
            await self.session.flush()
            return 0, 0

        created = 0
        for chunk_rows, content in _chunk_rows(rows, self.max_chars):
            self.session.add(TranscriptIndexChunkRow(
                transcript_id=transcript_id,
                index_version=INDEX_VERSION,
                start_sequence=chunk_rows[0].sequence,
                end_sequence=chunk_rows[-1].sequence,
                source_turn_ids=[str(row.id) for row in chunk_rows],
                content=content,
            ))
            created += 1
        state.last_indexed_sequence = rows[-1].sequence
        await self.session.flush()
        return created, len(rows)

    async def _active_cutoff(self, transcript: TranscriptRow, tail_exchanges: int) -> int:
        if transcript.closed_at is not None or tail_exchanges <= 0:
            return int(transcript.next_turn_sequence or 0)
        owner_sequences = list((await self.session.execute(
            select(TurnRow.sequence)
            .where(TurnRow.transcript_id == transcript.id, TurnRow.actor == "owner")
            .order_by(TurnRow.sequence.desc())
            .limit(tail_exchanges)
        )).scalars())
        if len(owner_sequences) < tail_exchanges:
            return 0
        return max(0, min(owner_sequences) - 1)

    async def run_once(self, *, active_tail_exchanges: int = 10) -> IndexRunResult:
        transcripts = list((await self.session.execute(
            select(TranscriptRow).order_by(TranscriptRow.created_at, TranscriptRow.id)
        )).scalars())
        advanced = 0
        chunks = 0
        turns = 0
        for transcript in transcripts:
            cutoff = await self._active_cutoff(transcript, active_tail_exchanges)
            if cutoff <= 0:
                continue
            created, processed = await self.index_transcript(
                transcript.id, through_sequence=cutoff
            )
            if processed:
                advanced += 1
                chunks += created
                turns += processed
        return IndexRunResult(
            transcripts_seen=len(transcripts),
            transcripts_advanced=advanced,
            chunks_created=chunks,
            turns_processed=turns,
        )
