import json
from uuid import UUID

from atlas.runtime.bootstrap import build_seat_bootstrap
from atlas.transcript.models import Actor, Turn


def build_model_instructions(capability_index: list[dict[str, str]] | None = None) -> str:
    seat = build_seat_bootstrap()
    capabilities = capability_index or []
    capability_text = ", ".join(item["family"] for item in capabilities) or "none"
    return (
        f"{seat.principle} "
        "You are speaking directly with your owner, Jaco. "
        "Be useful, concise when the task is simple, and explicit about uncertainty. "
        "The conversation messages supplied with each request are an Atlas-selected working projection of canonical transcript history and may span runtime restarts. "
        "Structured runtime evidence may also be supplied as developer messages; treat it as durable evidence of what Atlas actually did. "
        "Compacted runtime evidence is an intentional projection of fuller canonical evidence, not proof that the underlying evidence is unavailable. "
        "Treat supplied earlier turns as available history; do not claim they are unavailable merely because the owner mentions a restart. "
        "If no explicit restart marker is present, say you cannot identify the exact restart boundary rather than claiming the prior conversation is inaccessible. "
        "Do not claim to have tools or capabilities that Atlas has not exposed to you. "
        f"Enabled capability families currently visible through Atlas are: {capability_text}. "
        "When a task needs environment access, search the capability registry rather than guessing operation names. "
        "Tool calls describe operational intent; approval-required calls are prepared for the owner instead of being denied."
    )


def context_turns(turns: list[Turn], summarized_through_turn_id: UUID | None = None) -> list[Turn]:
    if summarized_through_turn_id is None:
        return turns
    for index, turn in enumerate(turns):
        if turn.id == summarized_through_turn_id:
            return turns[index + 1 :]
    return turns


def recent_exchange_turns(turns: list[Turn], exchange_count: int) -> list[Turn]:
    if exchange_count <= 0:
        return []
    owner_indexes = [index for index, turn in enumerate(turns) if turn.actor == Actor.OWNER]
    if not owner_indexes or len(owner_indexes) <= exchange_count:
        return turns
    return turns[owner_indexes[-exchange_count] :]


def tool_turn_exchange_ages(turns: list[Turn]) -> dict[UUID, int]:
    owner_sequence = 0
    tool_sequences: dict[UUID, int] = {}
    for turn in turns:
        if turn.actor == Actor.OWNER:
            owner_sequence += 1
        elif turn.actor == Actor.TOOL:
            tool_sequences[turn.id] = owner_sequence
    return {
        turn_id: max(1, owner_sequence - sequence + 1)
        for turn_id, sequence in tool_sequences.items()
    }


def tool_observation_is_compactable(block) -> bool:
    phase = str(getattr(block, "phase", None) or "observed").casefold()
    protected = {"failed", "uncertain", "prepared", "executing", "approval_required", "forbidden", "unavailable"}
    return phase not in protected


def _safe_compact_detail(detail: dict) -> str:
    fields: list[str] = []

    def add(key: str, value) -> None:
        if value is None or isinstance(value, (dict, list)):
            return
        text = str(value).strip()
        if text and len(text) <= 180 and f"{key}={text}" not in fields:
            fields.append(f"{key}={text}")

    add("status", detail.get("status"))
    add("query", detail.get("query"))
    output = detail.get("output") if isinstance(detail.get("output"), dict) else {}
    resource = output.get("resource") if isinstance(output.get("resource"), dict) else {}
    for source in (detail, output, resource):
        for key in ("project", "path", "name", "change", "failure_phase"):
            add(key, source.get(key))
    entries = output.get("entries") if isinstance(output, dict) else None
    if isinstance(entries, list):
        fields.append(f"entries={len(entries)}")
    result = detail.get("result") if isinstance(detail.get("result"), dict) else {}
    operations = result.get("operations") if isinstance(result, dict) else None
    if isinstance(operations, list):
        fields.append(f"operations={len(operations)}")
    return " · ".join(fields[:6])


def tool_observation_to_provider_message(block, *, compact: bool = False) -> dict[str, str]:
    detail = getattr(block, "detail", {}) or {}
    operation = getattr(block, "operation", None) or "runtime"
    phase = getattr(block, "phase", None) or "observed"
    if compact:
        summary = str(getattr(block, "summary", None) or "").strip()
        safe_detail = _safe_compact_detail(detail)
        parts = [f"Durable runtime evidence (compacted): {operation} [{phase}]"]
        if summary and summary not in {operation, f"{operation} · {phase}"}:
            parts.append(summary[:240])
        if safe_detail:
            parts.append(safe_detail)
        parts.append("full canonical observation retained")
        return {"role": "developer", "content": " · ".join(parts)}

    encoded = json.dumps(detail, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(encoded) > 6000:
        encoded = encoded[:6000] + "…"
    return {
        "role": "developer",
        "content": f"Durable runtime evidence: {operation} [{phase}] {encoded}",
    }


def turns_to_provider_messages(
    turns: list[Turn],
    *,
    context_summary: str | None = None,
    summarized_through_turn_id: UUID | None = None,
    compact_tool_turn_ids: set[UUID] | None = None,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    compact_ids = compact_tool_turn_ids or set()
    if context_summary:
        messages.append({
            "role": "developer",
            "content": "Atlas durable context capsule from earlier canonical history:\n" + context_summary,
        })
    for turn in context_turns(turns, summarized_through_turn_id):
        if turn.actor in (Actor.OWNER, Actor.ATLAS):
            text = "\n".join(
                block.text for block in turn.blocks if getattr(block, "type", None) == "text"
            ).strip()
            if text:
                messages.append({
                    "role": "user" if turn.actor == Actor.OWNER else "assistant",
                    "content": text,
                })
            continue
        if turn.actor == Actor.TOOL:
            for block in turn.blocks:
                if getattr(block, "type", None) != "tool_observation":
                    continue
                messages.append(tool_observation_to_provider_message(
                    block, compact=turn.id in compact_ids
                ))
    return messages
