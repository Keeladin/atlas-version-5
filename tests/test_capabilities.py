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
