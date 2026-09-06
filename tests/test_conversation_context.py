from uuid import uuid4

from atlas.runtime.conversation import context_turns, turns_to_provider_messages
from atlas.transcript.models import Actor, TextBlock, ToolObservationBlock, Turn


def _turn(actor: Actor, block) -> Turn:
    return Turn(transcript_id=uuid4(), actor=actor, blocks=[block])


def test_tool_observations_project_as_runtime_evidence() -> None:
    turn = _turn(
        Actor.TOOL,
        ToolObservationBlock(operation="gmail.message.send", phase="succeeded", detail={"id": "msg-1"}),
    )
    messages = turns_to_provider_messages([turn])
    assert messages == [{
        "role": "developer",
        "content": 'Durable runtime evidence: gmail.message.send [succeeded] {"id":"msg-1"}',
    }]


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
