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
        "The conversation messages supplied with each request are Atlas-owned canonical transcript history and may span runtime restarts. "
        "Structured runtime evidence may also be supplied as developer messages; treat it as durable evidence of what Atlas actually did. "
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


def turns_to_provider_messages(
    turns: list[Turn],
    *,
    context_summary: str | None = None,
    summarized_through_turn_id: UUID | None = None,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
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
                detail = getattr(block, "detail", {}) or {}
                encoded = json.dumps(detail, ensure_ascii=False, default=str, separators=(",", ":"))
                if len(encoded) > 6000:
                    encoded = encoded[:6000] + "…"
                operation = getattr(block, "operation", None) or "runtime"
                phase = getattr(block, "phase", None) or "observed"
                messages.append({
                    "role": "developer",
                    "content": f"Durable runtime evidence: {operation} [{phase}] {encoded}",
                })
    return messages
