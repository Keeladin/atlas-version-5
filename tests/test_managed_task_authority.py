import pytest
from atlas.capabilities import AuthorityMode, CapabilityRuntime, EffectKind, OperationDescriptor
from atlas.runtime.task_state import active_task_provider_message, new_managed_task_state


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
