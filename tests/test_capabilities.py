import pytest
from atlas.capabilities import (
    AuthorityMode,
    CapabilityRuntime,
    EffectKind,
    OperationDescriptor,
)


def descriptor(authority: AuthorityMode = AuthorityMode.AUTO) -> OperationDescriptor:
    return OperationDescriptor(
        id="test.read",
        capability_id="test.capability",
        family="Test",
        description="Read test data.",
        input_schema={"type": "object"},
        effect=EffectKind.READ,
        authority=authority,
    )


def test_capability_search_is_progressive_and_compact() -> None:
    runtime = CapabilityRuntime()
    runtime.register(descriptor(), lambda arguments: {"ok": True})
    assert runtime.compact_index() == [{"family": "Test", "capability_id": "test.capability"}]
    assert [item.id for item in runtime.search("read data")] == ["test.read"]


@pytest.mark.asyncio
async def test_auto_capability_executes() -> None:
    runtime = CapabilityRuntime()
    runtime.register(descriptor(), lambda arguments: {"path": arguments.get("path", "")})
    result = await runtime.call("test.read", {"path": "Docs"})
    assert result.status == "succeeded"
    assert result.output == {"path": "Docs"}


@pytest.mark.asyncio
async def test_approval_capability_prepares_instead_of_denies() -> None:
    runtime = CapabilityRuntime()
    runtime.register(descriptor(AuthorityMode.APPROVAL_REQUIRED), lambda arguments: {"changed": True})

    async def sink(operation, arguments):
        assert operation.id == "test.read"
        assert arguments == {"target": "x"}
        return "proposal-1"

    result = await runtime.call("test.read", {"target": "x"}, proposal_sink=sink)
    assert result.status == "approval_required"
    assert result.proposal_id == "proposal-1"


def test_argument_validation_rejects_missing_or_extra_fields() -> None:
    runtime = CapabilityRuntime()
    item = descriptor()
    item.input_schema = {
        "type": "object",
        "properties": {"target": {"type": "string"}},
        "required": ["target"],
        "additionalProperties": False,
    }
    runtime.register(item, lambda arguments: arguments)
    assert runtime.validate_arguments("test.read", {}) == "Missing required argument(s): target"
    assert runtime.validate_arguments("test.read", {"target": "x", "other": 1}) == "Unexpected argument(s): other"
    assert runtime.validate_arguments("test.read", {"target": "x"}) is None


def test_capability_search_cards_omit_full_schema_but_keep_argument_contract() -> None:
    runtime = CapabilityRuntime()
    item = descriptor()
    item.input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "very long schema prose"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    runtime.register(item, lambda arguments: arguments)

    card = runtime.search_cards("read data")[0]

    assert card["id"] == "test.read"
    assert card["arguments"] == {"path": "string"}
    assert card["required"] == ["path"]
    assert "input_schema" not in card
    assert "very long schema prose" not in str(card)


@pytest.mark.asyncio
async def test_complete_schema_validation_precedes_proposal_and_dispatch():
    item = descriptor(AuthorityMode.APPROVAL_REQUIRED)
    item.input_schema = {'type': 'object', 'properties': {'items': {'type': 'array',
        'items': {'type': 'integer', 'minimum': 2}}, 'mode': {'enum': ['safe']}},
        'required': ['items', 'mode'], 'additionalProperties': False}
    runtime = CapabilityRuntime()
    called = []
    runtime.register(item, lambda arguments: called.append('dispatch'))
    async def sink(*args):
        called.append('proposal')
        return 'proposal'
    for args in ({'items': [True], 'mode': 'safe'}, {'items': [1], 'mode': 'safe'},
                 {'items': [2], 'mode': 'other'}, {'items': '2', 'mode': 'safe'}):
        result = await runtime.call(item.id, args, proposal_sink=sink)
        assert result.status == 'failed'
        assert result.output['failure_phase'] == 'before_dispatch'
    assert called == []
    result = await runtime.call(item.id, {'items': [2], 'mode': 'safe'}, proposal_sink=sink)
    assert result.status == 'approval_required'
    assert called == ['proposal']


@pytest.mark.asyncio
async def test_value_error_after_effect_is_ambiguous():
    item = descriptor()
    item.effect = EffectKind.CREATE
    effects = []
    def execute(args):
        effects.append('created')
        raise ValueError('response decode failed')
    runtime = CapabilityRuntime()
    runtime.register(item, execute)
    result = await runtime.call(item.id, {})
    assert effects == ['created']
    assert result.output['failure_phase'] == 'ambiguous_dispatch'


@pytest.mark.asyncio
async def test_authority_resolver_refines_static_authority_per_call():

    item = descriptor(AuthorityMode.APPROVAL_REQUIRED)
    item.effect = EffectKind.EXECUTE
    calls = []

    def resolve(arguments):
        return AuthorityMode.AUTO if arguments.get('state') == 'restart' else AuthorityMode.APPROVAL_REQUIRED

    runtime = CapabilityRuntime()
    runtime.register(item, lambda arguments: calls.append(arguments) or {'ok': True}, authority_resolver=resolve)

    async def sink(operation, arguments):
        return 'proposal-1'

    auto = await runtime.call(item.id, {'state': 'restart'}, proposal_sink=sink)
    gated = await runtime.call(item.id, {'state': 'stop'}, proposal_sink=sink)
    assert auto.status == 'succeeded' and calls == [{'state': 'restart'}]
    assert gated.status == 'approval_required' and gated.proposal_id == 'proposal-1'
    assert await runtime.resolve_authority(item.id, {'state': 'stop'}) == AuthorityMode.APPROVAL_REQUIRED
    # A pre-resolved authority is honoured as-is (the executor resolves once per tool call).
    forced = await runtime.call(item.id, {'state': 'stop'}, proposal_sink=sink, authority=AuthorityMode.AUTO)
    assert forced.status == 'succeeded'


@pytest.mark.asyncio
async def test_authority_resolver_cannot_relax_forbidden_and_fails_closed():
    forbidden = descriptor(AuthorityMode.FORBIDDEN)
    runtime = CapabilityRuntime()
    runtime.register(forbidden, lambda arguments: {'ok': True}, authority_resolver=lambda arguments: AuthorityMode.AUTO)
    assert (await runtime.call(forbidden.id, {}, approval_granted=True)).status == 'forbidden'

    dynamic = descriptor()
    dynamic.id = 'test.dynamic'
    runtime.register(dynamic, lambda arguments: {'ok': True}, authority_resolver=lambda arguments: AuthorityMode.FORBIDDEN)
    assert (await runtime.call('test.dynamic', {}, approval_granted=True)).status == 'forbidden'

    broken = descriptor()
    broken.id = 'test.broken'
    executed = []

    def explode(arguments):
        raise LookupError('policy missing')

    runtime.register(broken, lambda arguments: executed.append(1), authority_resolver=explode)
    result = await runtime.call('test.broken', {})
    assert result.status == 'failed' and result.output['failure_phase'] == 'before_dispatch'
    assert 'policy missing' in (result.message or '') and executed == []


@pytest.mark.asyncio
async def test_definite_failures_are_failed_not_uncertain():
    from atlas.actions.models import ActionStatus
    from atlas.capabilities import CapabilityFailure
    from atlas.runtime.execution import action_status_for_result

    item = descriptor()
    item.effect = EffectKind.EXECUTE
    runtime = CapabilityRuntime()

    def execute(arguments):
        if arguments.get('mode') == 'refused':
            raise CapabilityFailure('nothing started', phase='before_dispatch', output={'reason': 'unreachable'})
        raise CapabilityFailure('exit status 1', phase='completed', output={'stderr': 'boom'})

    runtime.register(item, execute)
    completed = await runtime.call(item.id, {})
    refused = await runtime.call(item.id, {'mode': 'refused'})
    assert completed.status == 'failed' and completed.output == {'stderr': 'boom', 'failure_phase': 'completed'}
    assert refused.output['failure_phase'] == 'before_dispatch' and refused.output['reason'] == 'unreachable'
    assert action_status_for_result(completed) == ActionStatus.FAILED
    assert action_status_for_result(refused) == ActionStatus.FAILED


@pytest.mark.asyncio
async def test_owner_override_wins_over_defaults_rules_and_static_forbidden():
    gated = descriptor(AuthorityMode.APPROVAL_REQUIRED)
    gated.effect = EffectKind.EXECUTE
    locked = descriptor(AuthorityMode.FORBIDDEN)
    locked.id = 'test.locked'
    ruled = descriptor(AuthorityMode.AUTO)
    ruled.id = 'test.ruled'
    runtime = CapabilityRuntime()
    runtime.register(gated, lambda arguments: {'ok': 'gated'})
    runtime.register(locked, lambda arguments: {'ok': 'locked'})
    runtime.register(ruled, lambda arguments: {'ok': 'ruled'}, authority_resolver=lambda arguments: AuthorityMode.APPROVAL_REQUIRED)
    decisions = {}

    async def reader():
        return dict(decisions)

    runtime.authority_reader = reader

    async def sink(operation, arguments):
        return 'proposal'

    assert (await runtime.call(gated.id, {}, proposal_sink=sink)).status == 'approval_required'
    decisions[gated.id] = 'auto'
    assert (await runtime.call(gated.id, {}, proposal_sink=sink)).status == 'succeeded'
    decisions[gated.id] = 'forbidden'
    assert (await runtime.call(gated.id, {}, approval_granted=True)).status == 'forbidden'
    assert (await runtime.call(locked.id, {})).status == 'forbidden'
    decisions[locked.id] = 'auto'
    assert (await runtime.call(locked.id, {})).status == 'succeeded'
    assert (await runtime.call(ruled.id, {}, proposal_sink=sink)).status == 'approval_required'
    decisions[ruled.id] = 'auto'
    assert (await runtime.call(ruled.id, {}, proposal_sink=sink)).status == 'succeeded'
    decisions['test.bogus'] = 'loud'
    assert await runtime.authority_overrides() == {gated.id: AuthorityMode.FORBIDDEN, locked.id: AuthorityMode.AUTO, ruled.id: AuthorityMode.AUTO}
    cards = {card['id']: card for card in await runtime.search_cards_current('test')}
    assert cards[locked.id]['authority'] == 'auto' and cards[gated.id]['authority'] == 'forbidden'
    assert (await runtime.descriptor_current(locked.id)).authority == AuthorityMode.AUTO
    assert runtime.has_authority_resolver(ruled.id) and not runtime.has_authority_resolver(gated.id)
