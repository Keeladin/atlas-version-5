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
        "Treat supplied earlier turns as available history; do not claim they are unavailable merely because the owner mentions a restart. "
        "If no explicit restart marker is present, say you cannot identify the exact restart boundary rather than claiming the prior conversation is inaccessible. "
        "Do not claim to have tools or capabilities that Atlas has not exposed to you. "
        f"Enabled capability families currently visible through Atlas are: {capability_text}. "
        "When a task needs environment access, search the capability registry rather than guessing operation names. "
        "Tool calls describe operational intent; approval-required calls are prepared for the owner instead of being denied."
    )


def turns_to_provider_messages(turns: list[Turn]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for turn in turns:
        if turn.actor not in (Actor.OWNER, Actor.ATLAS):
            continue
        text = "\n".join(
            block.text for block in turn.blocks if getattr(block, "type", None) == "text"
        ).strip()
        if text:
            messages.append({
                "role": "user" if turn.actor == Actor.OWNER else "assistant",
                "content": text,
            })
    return messages
