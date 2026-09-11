from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from atlas.persistence.models import MemoryDiscoveryStateRow, TranscriptRow, TurnRow
from atlas.runtime.conversation import memory_evidence_handle_map

from .candidates import MemoryCandidate, MemoryCandidateIntake


class DiscoveryModel(Protocol):
    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str: ...


class DiscoveryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=8)


@dataclass(frozen=True)
class DiscoveryRunResult:
    transcripts_seen: int = 0
    turns_scanned: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    failures: int = 0


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


class MemoryBackgroundDiscoveryService:
    """Bounded recall sweep. Foreground proposals remain hints, never the only intake path."""
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
        }
        transcript_ids = await self._eligible_transcripts()
        for transcript_id in transcript_ids:
            stats["transcripts_seen"] += 1
            try:
                snapshot = await self._snapshot(transcript_id)
                if snapshot is None:
                    continue
                scan_turns, visible_turns, source_revision = snapshot
                stats["turns_scanned"] += len(scan_turns)
                handle_map = memory_evidence_handle_map(visible_turns)
                batch = await self._discover(visible_turns, handle_map)
                anchor = scan_turns[-1]
                sweep_horizon = max(
                    (turn.created_at for turn in scan_turns if turn.created_at is not None),
                    default=None,
                )
                intake = await self.intake.enqueue_many(
                    [item.model_dump(mode="json") for item in batch.memory_candidates],
                    source_transcript_id=transcript_id,
                    source_turn_id=anchor.id,
                    source_provider_evidence_id=None,
                    allowed_evidence_turn_ids={turn.id for turn in visible_turns},
                    evidence_handle_map=handle_map,
                    proposer_model=str(getattr(self.model, "model", "") or "") or None,
                    intake_path="sweep",
                    temporal_horizon_at=sweep_horizon,
                )
                stats["accepted"] += intake["accepted"]
                stats["duplicates"] += intake["duplicate"]
                stats["rejected"] += intake["rejected"]
                await self._advance(
                    transcript_id,
                    sequence=int(anchor.sequence),
                    source_revision=source_revision,
                )
            except Exception:  # noqa: BLE001 - one transcript must not stop the sweep
                stats["failures"] += 1
        return DiscoveryRunResult(**stats)

    async def _eligible_transcripts(self) -> list[UUID]:
        async with self.factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(TranscriptRow.id)
                        .outerjoin(
                            MemoryDiscoveryStateRow,
                            MemoryDiscoveryStateRow.transcript_id == TranscriptRow.id,
                        )
                        .where(
                            TranscriptRow.kind == "owner",
                            TranscriptRow.next_turn_sequence
                            > func.coalesce(
                                MemoryDiscoveryStateRow.last_scanned_sequence, 0
                            ),
                        )
                        .order_by(TranscriptRow.updated_at.asc(), TranscriptRow.id.asc())
                        .limit(self.max_transcripts)
                    )
                ).scalars()
            )
        return rows

    async def _snapshot(
        self, transcript_id: UUID
    ) -> tuple[list[TurnRow], list[TurnRow], int] | None:
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
            return scan_turns, visible_turns, int(transcript.content_revision or 0)

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
            "evidence_refs. Each evidence_refs item contains only handle. proposed_action is always upsert. No markdown or explanation."
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

    async def _advance(
        self, transcript_id: UUID, *, sequence: int, source_revision: int
    ) -> None:
        async with self.factory() as session, session.begin():
            transcript = await session.get(TranscriptRow, transcript_id)
            if transcript is None:
                return
            state = await session.get(MemoryDiscoveryStateRow, transcript_id)
            if state is None:
                state = MemoryDiscoveryStateRow(
                    transcript_id=transcript_id,
                    last_scanned_sequence=sequence,
                    source_revision=source_revision,
                )
                session.add(state)
                return
            state.last_scanned_sequence = max(
                int(state.last_scanned_sequence or 0), sequence
            )
            state.source_revision = max(
                int(state.source_revision or 0), source_revision
            )
