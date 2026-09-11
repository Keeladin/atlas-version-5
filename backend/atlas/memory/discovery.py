from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from atlas.persistence.models import MemoryDiscoveryStateRow, TranscriptRow, TurnRow
from atlas.runtime.conversation import memory_evidence_handle_map
from atlas.transcript.models import Turn
from atlas.transcript.repository import turn_from_row

from .candidates import MemoryCandidateIntake
from .durable import utcnow

logger = logging.getLogger(__name__)


class DiscoveryModel(Protocol):
    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str: ...


class DiscoveryBatch(BaseModel):
    """Container shape only. Items stay untyped here so that each proposal, including a
    non-object item, is validated individually at intake and rejects only itself."""

    model_config = ConfigDict(extra="forbid")
    memory_candidates: list[object] = Field(default_factory=list, max_length=8)


@dataclass(frozen=True)
class DiscoveryRunResult:
    transcripts_seen: int = 0
    turns_scanned: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    failures: int = 0
    stale: int = 0
    rejections: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoverySnapshot:
    """One bounded scan window read under the cursor it was taken at."""

    cursor: int
    scan_turns: list[TurnRow]
    visible_turns: list[TurnRow]
    content_revision: int


def _turn_projection(
    turn: TurnRow, reverse_handles: dict[tuple[UUID, str], str]
) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for index, block in enumerate(turn.blocks or []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = " ".join(str(block.get("text") or "").split()).strip()
            if text:
                handle = reverse_handles.get((turn.id, f"text:{index}"))
                if handle:
                    items.append({"handle": handle, "text": text[:4_000]})
        elif block.get("type") == "tool_observation":
            operation = str(block.get("operation") or "runtime")[:128]
            phase = str(block.get("phase") or "observed")[:64]
            summary = " ".join(str(block.get("summary") or "").split())[:500]
            handle = reverse_handles.get((turn.id, ""))
            if handle:
                items.append({
                    "handle": handle,
                    "text": f"{operation} [{phase}]" + (f": {summary}" if summary else ""),
                })
    return {
        "sequence": int(turn.sequence),
        "actor": turn.actor,
        "recorded_at": turn.created_at,
        "items": items,
    }


def _domain_turns(rows: list[TurnRow]) -> list[Turn]:
    """Evidence handles are minted from domain turns, exactly as in the foreground.

    A malformed legacy row simply gets no handles; it must not sink the sweep.
    """
    turns: list[Turn] = []
    for row in rows:
        try:
            turns.append(turn_from_row(row))
        except ValidationError:
            continue
    return turns


class MemoryBackgroundDiscoveryService:
    """Bounded recall sweep. Foreground proposals remain hints, never the only intake path.

    Cursor semantics: one ``memory_discovery_state`` row per owner transcript holds the
    last scanned turn sequence. Each run scans at most ``max_turns_per_transcript`` live
    turns after the cursor, shows up to ``context_turns`` earlier turns as citeable context,
    anchors every candidate on the last scanned turn, and advances the cursor only if it is
    still where the snapshot read it and no scanned turn was purged meanwhile. Concurrent
    runs are not leased; the loser of that check re-scans and intake deduplicates.
    """

    def __init__(
        self,
        factory: async_sessionmaker,
        model: DiscoveryModel,
        *,
        max_transcripts: int = 4,
        max_turns_per_transcript: int = 12,
        context_turns: int = 4,
    ) -> None:
        self.factory = factory
        self.model = model
        self.max_transcripts = max(1, min(int(max_transcripts), 16))
        self.max_turns_per_transcript = max(1, min(int(max_turns_per_transcript), 40))
        self.context_turns = max(0, min(int(context_turns), 12))
        self.intake = MemoryCandidateIntake(factory)

    async def run_once(self) -> DiscoveryRunResult:
        stats = {
            "transcripts_seen": 0,
            "turns_scanned": 0,
            "accepted": 0,
            "duplicates": 0,
            "rejected": 0,
            "failures": 0,
            "stale": 0,
        }
        rejections: dict[str, int] = {}
        transcript_ids = await self._eligible_transcripts()
        for transcript_id in transcript_ids:
            stats["transcripts_seen"] += 1
            try:
                snapshot = await self._snapshot(transcript_id)
                if snapshot is None:
                    continue
                stats["turns_scanned"] += len(snapshot.scan_turns)
                handle_map = memory_evidence_handle_map(_domain_turns(snapshot.visible_turns))
                batch = await self._discover(snapshot.visible_turns, handle_map)
                anchor = snapshot.scan_turns[-1]
                sweep_horizon = max(
                    (
                        turn.created_at
                        for turn in snapshot.scan_turns
                        if turn.created_at is not None
                    ),
                    default=None,
                )
                intake = await self.intake.enqueue_many(
                    list(batch.memory_candidates),
                    source_transcript_id=transcript_id,
                    source_turn_id=anchor.id,
                    source_provider_evidence_id=None,
                    allowed_evidence_turn_ids={turn.id for turn in snapshot.visible_turns},
                    evidence_handle_map=handle_map,
                    proposer_model=str(getattr(self.model, "model", "") or "") or None,
                    intake_path="sweep",
                    temporal_horizon_at=sweep_horizon,
                )
                stats["accepted"] += intake["accepted"]
                stats["duplicates"] += intake["duplicate"]
                stats["rejected"] += intake["rejected"]
                for category, count in (intake.get("rejections") or {}).items():
                    rejections[category] = rejections.get(category, 0) + int(count)
                if intake["rejected"]:
                    logger.warning(
                        "memory discovery rejected %d of %d proposals for transcript %s: %s",
                        intake["rejected"], len(batch.memory_candidates), transcript_id,
                        intake.get("rejections"),
                    )
                advanced = await self._advance(
                    transcript_id,
                    sequence=int(anchor.sequence),
                    expected_cursor=snapshot.cursor,
                    window_turn_ids={turn.id for turn in snapshot.scan_turns},
                    source_revision=snapshot.content_revision,
                )
                if not advanced:
                    stats["stale"] += 1
                    await self._touch(transcript_id)
            except Exception:
                logger.exception("memory discovery sweep failed for transcript %s", transcript_id)
                stats["failures"] += 1
                try:
                    await self._touch(transcript_id)
                except Exception:
                    logger.exception("could not record sweep attempt for %s", transcript_id)
        return DiscoveryRunResult(**stats, rejections=dict(sorted(rejections.items())))

    async def _eligible_transcripts(self) -> list[UUID]:
        cursor = func.coalesce(MemoryDiscoveryStateRow.last_scanned_sequence, 0)
        unscanned_live_turn = (
            select(TurnRow.id)
            .where(
                TurnRow.transcript_id == TranscriptRow.id,
                TurnRow.sequence > cursor,
                TurnRow.deleted_at.is_(None),
            )
            .exists()
        )
        async with self.factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(TranscriptRow.id)
                        .outerjoin(
                            MemoryDiscoveryStateRow,
                            MemoryDiscoveryStateRow.transcript_id == TranscriptRow.id,
                        )
                        .where(TranscriptRow.kind == "owner", unscanned_live_turn)
                        .order_by(
                            # Never-swept first, then least recently attempted.
                            MemoryDiscoveryStateRow.updated_at.asc().nulls_first(),
                            TranscriptRow.updated_at.asc(),
                            TranscriptRow.id.asc(),
                        )
                        .limit(self.max_transcripts)
                    )
                ).scalars()
            )
        return rows

    async def _snapshot(self, transcript_id: UUID) -> DiscoverySnapshot | None:
        async with self.factory() as session:
            transcript = await session.get(TranscriptRow, transcript_id)
            if transcript is None:
                return None
            state = await session.get(MemoryDiscoveryStateRow, transcript_id)
            last_sequence = int(state.last_scanned_sequence or 0) if state else 0
            scan_turns = list(
                (
                    await session.execute(
                        select(TurnRow)
                        .where(
                            TurnRow.transcript_id == transcript_id,
                            TurnRow.sequence > last_sequence,
                            TurnRow.deleted_at.is_(None),
                        )
                        .order_by(TurnRow.sequence.asc())
                        .limit(self.max_turns_per_transcript)
                    )
                ).scalars()
            )
            if not scan_turns:
                return None
            first_sequence = int(scan_turns[0].sequence)
            visible_start = max(1, first_sequence - self.context_turns)
            visible_turns = list(
                (
                    await session.execute(
                        select(TurnRow)
                        .where(
                            TurnRow.transcript_id == transcript_id,
                            TurnRow.sequence >= visible_start,
                            TurnRow.sequence <= int(scan_turns[-1].sequence),
                            TurnRow.deleted_at.is_(None),
                        )
                        .order_by(TurnRow.sequence.asc())
                    )
                ).scalars()
            )
            return DiscoverySnapshot(
                cursor=last_sequence,
                scan_turns=scan_turns,
                visible_turns=visible_turns,
                content_revision=int(transcript.content_revision or 0),
            )

    async def _discover(
        self, turns: list[TurnRow], handle_map: dict[str, tuple[UUID, str]]
    ) -> DiscoveryBatch:
        instructions = (
            "You are Atlas's bounded background memory discovery sweep. Read the canonical "
            "turns and propose zero to eight memory candidates that may be useful beyond the "
            "immediate turn. This is discovery only; proposals have no memory authority. "
            "Every candidate must cite evidence_refs using only the supplied short handle. Assistant/model statements are not "
            "owner facts and must never be the sole authority for an owner claim; tool results "
            "are external observations, not owner statements. Prefer explicit owner corrections, "
            "stable preferences, durable decisions, and useful project state. Temporary current "
            "circumstances should be short_term. Do not use confidence as permission to store. "
            "Return exactly one JSON object with key memory_candidates. Candidate keys are kind, "
            "content, scope, confidence, durability, proposed_action, subject, namespace, and "
            "evidence_refs. Each evidence_refs item contains only handle. proposed_action is always upsert. "
            "Use only these exact values: kind is one of identity, preference, fact, decision, "
            "relationship, procedure, project_state, intent (a place or a goal is a fact or an intent, "
            "not a new kind); scope is one of chat, project, cross_chat (never global); durability is "
            "one of short_term, long_term; confidence is a number between 0 and 1, never a word. "
            "A candidate with any other value is rejected. No markdown or explanation."
        )
        raw = await self.model.complete_text(
            instructions=instructions,
            messages=[{
                "role": "user",
                "content": json.dumps(
                    {
                        "canonical_turns": [
                            _turn_projection(
                                turn, {value: key for key, value in handle_map.items()}
                            )
                            for turn in turns
                        ]
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            }],
        )
        text = raw.strip()
        if not (text.startswith("{") and text.endswith("}")):
            raise ValueError("memory discovery model did not return one JSON object")
        return DiscoveryBatch.model_validate(json.loads(text))

    @staticmethod
    async def _locked_state(session, transcript_id: UUID) -> MemoryDiscoveryStateRow:
        # Guarantee a row to lock so concurrent first sweeps serialize on it.
        await session.execute(
            pg_insert(MemoryDiscoveryStateRow)
            .values(transcript_id=transcript_id, last_scanned_sequence=0, source_revision=0)
            .on_conflict_do_nothing(index_elements=[MemoryDiscoveryStateRow.transcript_id])
        )
        return (
            await session.execute(
                select(MemoryDiscoveryStateRow)
                .where(MemoryDiscoveryStateRow.transcript_id == transcript_id)
                .with_for_update()
            )
        ).scalar_one()

    async def _advance(
        self,
        transcript_id: UUID,
        *,
        sequence: int,
        expected_cursor: int,
        window_turn_ids: set[UUID],
        source_revision: int,
    ) -> bool:
        """Compare-and-set the cursor.

        Refuses when the cursor moved since the snapshot (a concurrent run or a purge
        rewind) or when any scanned turn was purged meanwhile; the window is then
        re-scanned next run with the redacted turns excluded.
        """
        async with self.factory() as session, session.begin():
            state = await self._locked_state(session, transcript_id)
            if int(state.last_scanned_sequence or 0) != int(expected_cursor):
                return False
            if window_turn_ids:
                live = int((await session.execute(
                    select(func.count()).select_from(TurnRow).where(
                        TurnRow.id.in_(window_turn_ids),
                        TurnRow.deleted_at.is_(None),
                    )
                )).scalar_one())
                if live != len(window_turn_ids):
                    return False
            state.last_scanned_sequence = int(sequence)
            # Informational: the content revision the last successful sweep observed.
            state.source_revision = int(source_revision)
            state.updated_at = utcnow()
            return True

    async def _touch(self, transcript_id: UUID) -> None:
        """Record an attempt without moving the cursor so the transcript rotates back."""
        async with self.factory() as session, session.begin():
            state = await self._locked_state(session, transcript_id)
            state.updated_at = utcnow()
