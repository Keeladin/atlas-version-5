from datetime import UTC, datetime
from uuid import uuid4

from atlas.runtime.conversation import (
    build_model_instructions,
    context_turns,
    recent_exchange_turns,
    tool_observation_is_compactable,
    turns_to_provider_messages,
)
from atlas.transcript.models import Actor, TextBlock, ToolObservationBlock, Turn


def _turn(actor: Actor, block) -> Turn:
    return Turn(transcript_id=uuid4(), actor=actor, blocks=[block])


def test_tool_observations_project_as_runtime_evidence() -> None:
    turn = _turn(
        Actor.TOOL,
        ToolObservationBlock(operation="gmail.message.send", phase="succeeded", detail={"id": "msg-1"}),
    )
    messages = turns_to_provider_messages([turn])
    assert messages[0]["role"] == "user"
    assert 'Untrusted source content inside runtime evidence' in messages[0]["content"]
    assert 'gmail.message.send [succeeded] {"id":"msg-1"}' in messages[0]["content"]
    assert f'evidence_id={turn.id}' in messages[0]["content"]



def test_context_summary_replaces_archived_prefix() -> None:
    first = _turn(Actor.OWNER, TextBlock(text="old"))
    second = _turn(Actor.ATLAS, TextBlock(text="new"))
    assert context_turns([first, second], first.id) == [second]
    messages = turns_to_provider_messages(
        [first, second], context_summary="Earlier context", summarized_through_turn_id=first.id,
    )
    assert messages[0]["role"] == "developer"
    assert "Earlier context" in messages[0]["content"]
    assert messages[1] == {"role": "assistant", "content": "new"}


def test_recent_exchange_window_counts_owner_exchanges_not_tool_turns() -> None:
    transcript_id = uuid4()
    turns = []
    for index in range(3):
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.OWNER, blocks=[TextBlock(text=f"owner {index}")]))
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.TOOL, blocks=[ToolObservationBlock(operation="demo", phase="succeeded", detail={"index": index})]))
        turns.append(Turn(transcript_id=transcript_id, actor=Actor.ATLAS, blocks=[TextBlock(text=f"atlas {index}")]))

    selected = recent_exchange_turns(turns, 2)

    assert selected[0].actor == Actor.OWNER
    assert selected[0].blocks[0].text == "owner 1"
    assert sum(turn.actor == Actor.OWNER for turn in selected) == 2
    assert sum(turn.actor == Actor.TOOL for turn in selected) == 2


def test_compacted_tool_projection_keeps_safe_structure_not_raw_payload() -> None:
    turn = _turn(
        Actor.TOOL,
        ToolObservationBlock(
            operation="storage.projects.acquire",
            phase="succeeded",
            detail={
                "status": "succeeded",
                "output": {"resource": {"path": "normalizer/README.md", "name": "README.md", "data_base64": "SECRET" * 1000}},
            },
        ),
    )

    messages = turns_to_provider_messages([turn], compact_tool_turn_ids={turn.id})

    assert "compacted" in messages[0]["content"]
    assert "normalizer/README.md" in messages[0]["content"]
    assert "SECRET" not in messages[0]["content"]
    assert "full canonical observation retained" in messages[0]["content"]


def test_failure_and_uncertainty_are_not_normal_compaction_candidates() -> None:
    assert tool_observation_is_compactable(ToolObservationBlock(phase="succeeded")) is True
    assert tool_observation_is_compactable(ToolObservationBlock(phase="searched")) is True
    assert tool_observation_is_compactable(ToolObservationBlock(phase="failed")) is False
    assert tool_observation_is_compactable(ToolObservationBlock(phase="uncertain")) is False


def test_model_instructions_prioritize_semantic_judgment_and_proportional_verification() -> None:
    instructions = build_model_instructions([], active_task_enabled=False)

    assert "Prioritize understanding his actual intent" in instructions
    assert "Use your own semantic judgment" in instructions
    assert "Say when you disagree" in instructions
    assert "Relevance determines whether something is worth saying" in instructions
    assert "confidence determines how strongly it is stated" in instructions
    assert "rather than withholding it" in instructions
    assert "CONVERSATIONAL is the default" in instructions
    assert "PRECISE applies" in instructions
    assert "FORENSIC applies" in instructions
    assert "State genuine uncertainty clearly but once" in instructions
    assert "Treat owner statements, prior Atlas/model statements" in instructions
    assert "Use familiarity earned from supplied context naturally" in instructions
    assert "Use memory capabilities for explicit owner requests" in instructions
    assert "memory.obligations.resolve" not in instructions
    assert "Never bulk-confirm memory reviews" not in instructions



def test_foreground_time_reference_uses_owner_timezone() -> None:
    instructions = build_model_instructions(
        [], active_task_enabled=False, owner_timezone="Africa/Johannesburg",
        now=datetime(2026, 9, 13, 5, 10, tzinfo=UTC),
    )

    assert "Current owner-local date and time: 2026-09-13 07:10 SAST" in instructions
    assert "Time is part of context" in instructions
    assert "elapsed time relative to the situation" in instructions
    assert "do not treat age alone" in instructions


def test_only_owner_turns_receive_local_timestamps() -> None:
    transcript_id = uuid4()
    owner = Turn(
        transcript_id=transcript_id, actor=Actor.OWNER, blocks=[TextBlock(text="hello")],
        created_at=datetime(2026, 9, 12, 20, 30, tzinfo=UTC),
    )
    atlas = Turn(
        transcript_id=transcript_id, actor=Actor.ATLAS, blocks=[TextBlock(text="hi")],
        created_at=datetime(2026, 9, 12, 20, 31, tzinfo=UTC),
    )

    messages = turns_to_provider_messages([owner, atlas], owner_timezone="Africa/Johannesburg")

    assert messages[0]["content"].startswith("[Owner turn timestamp: 2026-09-12 22:30 SAST]\n")
    assert messages[0]["content"].endswith("hello")
    assert messages[1] == {"role": "assistant", "content": "hi"}

def test_owner_retirement_redacts_guarded_content_from_working_projection() -> None:
    phrase = "Roses are red, violets are blue."
    owner = _turn(Actor.OWNER, TextBlock(text=f"Remember this phrase: {phrase}"))
    atlas = _turn(Actor.ATLAS, TextBlock(text=f"Got it: {phrase}"))
    tool = _turn(
        Actor.TOOL,
        ToolObservationBlock(
            operation="memory.search", phase="succeeded",
            detail={"results": [{"content": phrase}]},
        ),
    )

    messages = turns_to_provider_messages(
        [owner, atlas, tool],
        context_summary=f"The old phrase was {phrase}",
        suppressed_contents=[phrase],
    )
    projected = "\n".join(str(item["content"]) for item in messages)

    assert phrase not in projected
    assert projected.count("[suppressed by owner memory directive]") >= 4
