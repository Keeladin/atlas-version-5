from uuid import uuid4

from atlas.actions.models import Action, ActionStatus, Run, RunKind
from atlas.runtime.bootstrap import build_seat_bootstrap
from atlas.transcript.models import Actor, ArtifactRefBlock, TextBlock, Turn


def test_bootstrap_keeps_model_led_principle() -> None:
    bootstrap = build_seat_bootstrap()
    assert bootstrap.identity == "Atlas"
    assert "primary semantic decision-maker" in bootstrap.principle
    assert bootstrap.environment_registry_available is True


def test_turn_accepts_multimodal_content_blocks() -> None:
    turn = Turn(
        transcript_id=uuid4(),
        actor=Actor.OWNER,
        blocks=[TextBlock(text="Look at this"), ArtifactRefBlock(artifact_id=uuid4())],
    )
    assert [block.type for block in turn.blocks] == ["text", "artifact_ref"]


def test_action_can_be_marked_uncertain() -> None:
    run = Run(kind=RunKind.FOREGROUND)
    action = Action(run_id=run.id, operation="mail.send", target_hash="a" * 64)
    action.status = ActionStatus.UNCERTAIN
    assert action.status is ActionStatus.UNCERTAIN
