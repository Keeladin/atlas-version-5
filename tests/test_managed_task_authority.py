from uuid import UUID

import pytest
from atlas.artifacts.store import ArtifactStore
from atlas.capabilities import (
    AuthorityMode,
    CapabilityRuntime,
    EffectKind,
    OperationDescriptor,
)
from atlas.config import Settings
from atlas.integrations import workspace_tasks_mcp_server as workspace_mcp
from atlas.persistence.models import ActionRow, OwnerAttentionRow
from atlas.runtime import managed_tasks_worker as worker
from atlas.runtime.execution import RunExecutor
from atlas.runtime.task_state import (
    active_task_provider_message,
    new_managed_task_state,
)
from sqlalchemy import select


def _operation(operation_id: str, authority: AuthorityMode) -> OperationDescriptor:
    return OperationDescriptor(
        id=operation_id,
        capability_id="coding.agent",
        family="Coding agent",
        description=f"Test {operation_id}",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effect=EffectKind.EXECUTE,
        authority=authority,
    )


@pytest.mark.asyncio
async def test_task_grant_preflight_promotes_ask_me_but_never_control_deny():
    operation = _operation("coding.agent.start_session", AuthorityMode.APPROVAL_REQUIRED)
    runtime = CapabilityRuntime()
    runtime.register(operation, lambda arguments: {"ok": True})

    async def enabled():
        return {"coding.agent"}

    decisions = {}

    async def authorities():
        return dict(decisions)

    runtime.policy_reader = enabled
    runtime.authority_reader = authorities

    grants = await runtime.preflight_authority_grants([operation.id, operation.id])
    assert grants == [operation.id]
    assert await runtime.apply_task_grant(
        operation.id, AuthorityMode.APPROVAL_REQUIRED
    ) == AuthorityMode.AUTO

    decisions[operation.id] = AuthorityMode.FORBIDDEN.value
    with pytest.raises(ValueError, match="denied by current Atlas Control policy"):
        await runtime.preflight_authority_grants([operation.id])
    assert await runtime.apply_task_grant(
        operation.id, AuthorityMode.APPROVAL_REQUIRED
    ) == AuthorityMode.FORBIDDEN


@pytest.mark.asyncio
async def test_task_grant_preflight_rejects_unknown_disabled_and_default_forbidden():
    allowed = _operation("coding.agent.send_turn", AuthorityMode.APPROVAL_REQUIRED)
    forbidden = _operation("coding.agent.danger", AuthorityMode.FORBIDDEN)
    runtime = CapabilityRuntime()
    runtime.register(allowed, lambda arguments: {"ok": True})
    runtime.register(forbidden, lambda arguments: {"ok": True})

    async def enabled():
        return set()

    runtime.policy_reader = enabled
    with pytest.raises(ValueError, match="capability coding.agent is disabled"):
        await runtime.preflight_authority_grants([allowed.id])

    async def enabled_again():
        return {"coding.agent"}

    runtime.policy_reader = enabled_again
    with pytest.raises(ValueError, match="operation is not registered"):
        await runtime.preflight_authority_grants(["missing.operation"])
    with pytest.raises(ValueError, match="denied by current Atlas Control policy"):
        await runtime.preflight_authority_grants([forbidden.id])


@pytest.mark.asyncio
async def test_explicit_control_override_can_make_static_forbidden_task_grantable():
    operation = _operation("coding.agent.owner_override", AuthorityMode.FORBIDDEN)
    runtime = CapabilityRuntime()
    runtime.register(operation, lambda arguments: {"ok": True})

    async def authorities():
        return {operation.id: AuthorityMode.APPROVAL_REQUIRED.value}

    runtime.authority_reader = authorities
    assert await runtime.preflight_authority_grants([operation.id]) == [operation.id]
    resolved = await runtime.resolve_authority(operation.id, {})
    assert resolved == AuthorityMode.APPROVAL_REQUIRED
    assert await runtime.apply_task_grant(operation.id, resolved) == AuthorityMode.AUTO



@pytest.mark.asyncio
async def test_managed_task_lifecycle_operations_cannot_be_delegated():
    runtime = CapabilityRuntime()
    for operation_id in (
        "workspace.tasks.create",
        "workspace.tasks.cancel",
        "workspace.tasks.resume",
    ):
        runtime.register(
            OperationDescriptor(
                id=operation_id,
                capability_id="workspace.tasks",
                family="Workspace tasks",
                description=operation_id,
                input_schema={"type": "object", "properties": {}},
                effect=EffectKind.UPDATE,
                authority=AuthorityMode.APPROVAL_REQUIRED,
            ),
            lambda arguments: {"ok": True},
        )

    async def enabled():
        return {"workspace.tasks"}

    runtime.policy_reader = enabled
    with pytest.raises(ValueError, match="lifecycle authority cannot be delegated"):
        await runtime.preflight_authority_grants(["workspace.tasks.create"])
    assert await runtime.apply_task_grant(
        "workspace.tasks.create", AuthorityMode.APPROVAL_REQUIRED
    ) == AuthorityMode.APPROVAL_REQUIRED

def test_managed_task_checkpoint_exposes_immutable_authority_grants():
    state = new_managed_task_state(
        title="Implement Atlas",
        objective="Implement the approved change",
        scope=["Atlas V5 only"],
        acceptance_criteria=["Tests pass"],
        authority_grants=["coding.agent.start_session", "coding.agent.send_turn"],
    )
    assert state["authority_grants"] == [
        "coding.agent.start_session",
        "coding.agent.send_turn",
    ]
    message = active_task_provider_message(state)
    assert message is not None
    assert '"authority_grants":["coding.agent.start_session","coding.agent.send_turn"]' in message["content"]
    assert "may promote Ask me to Auto for those operations only" in message["content"]


@pytest.mark.asyncio
async def test_run_executor_uses_task_grant_without_creating_owner_prompt(
    pg_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr(workspace_mcp, "get_session_factory", lambda: pg_factory)
    monkeypatch.setattr(worker, "get_session_factory", lambda: pg_factory)
    operation_id = "coding.agent.test_effect"
    created = await workspace_mcp._create({
        "objective": "Execute the already approved coding effect",
        "acceptance_criteria": ["Effect runs without asking twice"],
        "authority_grants": [operation_id],
    })
    settings = Settings(
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "artifacts",
        database_url=None,
    )
    run_id = await worker._claim_one(settings)
    assert run_id is not None

    calls = []
    runtime = CapabilityRuntime()
    runtime.register(
        _operation(operation_id, AuthorityMode.APPROVAL_REQUIRED),
        lambda arguments: calls.append(arguments) or {"ok": True},
    )
    executor = RunExecutor(
        pg_factory,
        runtime,
        ArtifactStore(settings.artifact_dir),
        run_id=run_id,
        transcript_id=UUID(created["transcript_id"]),
        checkpoint=True,
    )

    result = await executor.tool_handler(
        "atlas_capability_call",
        {"operation_id": operation_id, "arguments": {}},
    )

    assert result["status"] == "succeeded"
    assert calls == [{}]
    async with pg_factory() as session:
        actions = list((await session.execute(
            select(ActionRow).where(ActionRow.run_id == run_id)
        )).scalars())
        attention = list((await session.execute(
            select(OwnerAttentionRow).where(OwnerAttentionRow.run_id == run_id)
        )).scalars())
    assert any(action.operation == operation_id and action.status == "succeeded" for action in actions)
    assert not any(item.state == "approval_required" for item in attention)
