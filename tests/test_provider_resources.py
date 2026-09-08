from atlas.providers.openai import _public_tool_result, _resource_input


def test_resource_payload_is_not_echoed_into_tool_context() -> None:
    result = {
        "status": "succeeded",
        "output": {
            "resource": {
                "name": "photo.jpg",
                "media_type": "image/jpeg",
                "source": "local_workspace",
                "data_base64": "SECRET-BYTES",
            }
        },
    }

    public, resource = _public_tool_result(result)

    assert resource is not None
    assert "data_base64" not in public["output"]["resource"]
    assert resource["data_base64"] == "SECRET-BYTES"


def test_image_resource_routes_to_native_image_input() -> None:
    item = _resource_input({
        "name": "photo.jpg",
        "media_type": "image/jpeg",
        "source": "local_workspace",
        "data_base64": "YWJj",
    })

    assert item is not None
    assert item["content"][1]["type"] == "input_image"
    assert item["content"][1]["image_url"] == "data:image/jpeg;base64,YWJj"


def test_native_web_search_is_available_to_model() -> None:
    from atlas.providers.openai import _MODEL_TOOLS

    assert {"type": "web_search"} in _MODEL_TOOLS


def test_octet_stream_utf8_resource_routes_to_text_input() -> None:
    item = _resource_input({
        "name": ".env.example",
        "media_type": "application/octet-stream",
        "source": "project_folder",
        "data_base64": "Rk9PPWJhcg==",
    })

    assert item is not None
    assert item["content"][0]["type"] == "input_text"
    assert "FOO=bar" in item["content"][0]["text"]
    assert all(block["type"] != "input_file" for block in item["content"])


def test_unknown_binary_resource_does_not_emit_unsupported_input_file() -> None:
    item = _resource_input({
        "name": "blob.bin",
        "media_type": "application/octet-stream",
        "source": "project_folder",
        "data_base64": "/wAB",
    })

    assert item is not None
    assert item["content"][0]["type"] == "input_text"
    assert all(block["type"] != "input_file" for block in item["content"])


def test_capability_budget_does_not_charge_search_and_reserves_completion_calls() -> None:
    from atlas.providers.openai import _CapabilityBudget

    budget = _CapabilityBudget(limit=4, reserve=2)
    budget.remember_search({"operations": [
        {"id": "storage.projects.acquire", "effect": "read"},
        {"id": "storage.projects.preview", "effect": "read"},
        {"id": "storage.projects.apply", "effect": "update"},
    ]})
    assert budget.dispatched == 0
    assert budget.can_dispatch("storage.projects.acquire")[0] is True
    budget.record_dispatch()
    budget.record_dispatch()
    assert budget.can_dispatch("storage.projects.acquire")[0] is False
    assert budget.can_dispatch("storage.projects.preview")[0] is True
    assert budget.can_dispatch("storage.projects.apply")[0] is True


def test_capability_budget_hard_limit_is_sixteen_by_default() -> None:
    from atlas.providers.openai import _CapabilityBudget

    budget = _CapabilityBudget()
    assert budget.limit == 16
    for _ in range(16):
        budget.record_dispatch()
    allowed, message = budget.can_dispatch("storage.projects.apply")
    assert allowed is False
    assert "limit" in str(message)


def test_count_input_tokens_uses_neutral_input_for_empty_messages() -> None:
    import asyncio
    from types import SimpleNamespace

    from atlas.providers.openai import OpenAIProvider

    class Counter:
        def __init__(self) -> None:
            self.kwargs = None

        async def count(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(input_tokens=123)

    counter = Counter()
    provider = object.__new__(OpenAIProvider)
    provider.model = "test-model"
    provider.client = SimpleNamespace(
        responses=SimpleNamespace(input_tokens=counter),
    )

    result = asyncio.run(provider.count_input_tokens(instructions="Atlas seat", messages=[]))

    assert result == 123
    assert counter.kwargs is not None
    assert counter.kwargs["input"] == [{"role": "user", "content": ""}]


def test_model_tool_projection_bounds_large_html_and_marks_compaction() -> None:
    result = {
        "status": "succeeded",
        "operation_id": "gmail.message.read",
        "output": {"body": "<html><body><p>hello</p>" + ("x" * 40000) + "</body></html>"},
    }

    public, resource = _public_tool_result(result)

    assert resource is None
    encoded = __import__("json").dumps(public)
    assert len(encoded) < 14000
    assert "<html>" not in encoded
    assert public["model_projection"]["compacted"] is True
    assert public["model_projection"]["canonical_evidence_retained"] is True


def test_task_state_envelope_is_hidden_from_visible_answer() -> None:
    from atlas.providers.openai import _extract_task_state_delta

    visible, delta = _extract_task_state_delta(
        'Done.\n<atlas_task_state_delta>{"next_step":"Run tests","status":"active"}</atlas_task_state_delta>'
    )

    assert visible == "Done."
    assert delta == {"next_step": "Run tests", "status": "active"}


def test_malformed_task_state_envelope_is_hidden_but_not_applied() -> None:
    from atlas.providers.openai import _extract_task_state_delta

    visible, delta = _extract_task_state_delta(
        "Answer\n<atlas_task_state_delta>{bad json}</atlas_task_state_delta>"
    )

    assert visible == "Answer"
    assert delta is None


def test_capability_search_cache_is_scoped_to_exact_turn_arguments() -> None:
    from atlas.providers.openai import _CapabilityBudget

    budget = _CapabilityBudget()
    arguments = {"query": "gmail latest email", "limit": 3}
    result = {"operations": [{"id": "gmail.messages.search", "effect": "read"}]}

    assert budget.cached_search(arguments) is None
    budget.cache_search(arguments, result)
    assert budget.cached_search(arguments) == result
    assert budget.cached_search({"query": "gmail latest email", "limit": 2}) is None
    assert budget.operation_effects["gmail.messages.search"] == "read"


def test_large_utf8_resource_is_capped_and_points_to_line_range_reacquisition() -> None:
    import base64

    item = _resource_input({
        "name": "large.txt",
        "media_type": "text/plain",
        "source": "project_folder",
        "data_base64": base64.b64encode(("x" * 100000).encode()).decode(),
    })

    assert item is not None
    text = item["content"][0]["text"]
    assert len(text) < 50000
    assert "start_line/max_lines" in text


def test_exact_evidence_result_does_not_normalize_markup():
    result = {'operation_id': 'evidence.read', 'output': {'text': '<script>  literal source </script>\n', 'exact': True}}
    assert _public_tool_result(result)[0] == result


def test_provider_captures_web_citations_and_delta_without_extra_inference():
    import asyncio
    from types import SimpleNamespace

    from atlas.providers.openai import OpenAIProvider
    class Item:
        def __init__(self, payload):
            self.payload, self.type = payload, payload['type']
        def model_dump(self, **kwargs): return self.payload
    web = {'type': 'web_search_call', 'id': 'web1', 'status': 'completed', 'action': {'type': 'search', 'query': 'fixture'}}
    message = {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Answer',
        'annotations': [{'type': 'url_citation', 'url': 'https://example.test/source', 'title': 'Source', 'start_index': 0, 'end_index': 6}]}]}
    calls, observations, deltas = [], [], []
    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id='response1', status='completed', output=[Item(web), Item(message)],
            output_text='Answer<atlas_task_state_delta>{"status":"complete"}</atlas_task_state_delta>')
    async def observe(payload): observations.append(payload)
    async def delta(payload): deltas.append(payload)
    async def tool(*args): raise AssertionError('No capability call expected')
    provider = object.__new__(OpenAIProvider)
    provider.model, provider.capability_call_limit, provider.capability_completion_reserve = 'fixture', 16, 2
    provider.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    async def run():
        return [chunk async for chunk in provider.stream_text(instructions='Fixture', messages=[],
            tool_handler=tool, observation_handler=observe, task_state_handler=delta)]
    assert asyncio.run(run()) == ['Answer']
    assert len(calls) == 1
    assert observations[0]['output'] == [web, message]
    assert observations[0]['response_id'] == 'response1'
    assert deltas == [{'status': 'complete'}]


def test_memory_context_suppression_metadata_is_not_model_visible() -> None:
    result = {
        "operation_id": "memory.forget",
        "status": "succeeded",
        "output": {
            "status": "applied",
            "memory_id": "memory-1",
            "_context_suppression": {"contents": ["Roses are red, violets are blue."]},
        },
    }

    public, resource = _public_tool_result(result)

    assert resource is None
    assert "_context_suppression" not in public["output"]
    assert "Roses are red" not in str(public)


def test_successful_forget_redacts_prior_tool_round_before_completion() -> None:
    import asyncio
    import json
    from types import SimpleNamespace

    from atlas.providers.openai import OpenAIProvider

    phrase = "Roses are red, violets are blue."

    class Call:
        type = "function_call"

        def __init__(self, call_id, operation, arguments):
            self.call_id = call_id
            self.name = "atlas_capability_call"
            self.arguments = json.dumps({"operation_id": operation, "arguments": arguments})

        def model_dump(self, **kwargs):
            return {
                "type": self.type, "call_id": self.call_id, "name": self.name,
                "arguments": self.arguments,
            }

    seen_inputs = []
    responses = [
        SimpleNamespace(id="r1", status="completed", output=[Call("c1", "memory.search", {"query": "phrase"})], output_text=""),
        SimpleNamespace(id="r2", status="completed", output=[Call("c2", "memory.forget", {"content": phrase})], output_text=""),
        SimpleNamespace(id="r3", status="completed", output=[], output_text="Forgotten."),
    ]

    async def create(**kwargs):
        seen_inputs.append(kwargs["input"])
        return responses.pop(0)

    async def tool(name, arguments):
        operation = arguments["operation_id"]
        if operation == "memory.search":
            return {
                "status": "succeeded", "operation_id": operation,
                "output": {"results": [{"content": phrase}]},
            }
        assert operation == "memory.forget"
        return {
            "status": "succeeded", "operation_id": operation,
            "output": {
                "status": "applied", "memory_id": "m1",
                "_context_suppression": {"contents": [phrase]},
            },
        }

    provider = object.__new__(OpenAIProvider)
    provider.model = "fixture"
    provider.capability_call_limit = 16
    provider.capability_completion_reserve = 2
    provider.input_token_budget = None
    provider.capability_policy = None
    provider.client = SimpleNamespace(responses=SimpleNamespace(create=create))

    async def run():
        return [
            chunk
            async for chunk in provider.stream_text(
                instructions="Fixture",
                messages=[{"role": "user", "content": f"Forget {phrase}"}],
                tool_handler=tool,
            )
        ]

    assert asyncio.run(run()) == ["Forgotten."]
    final_input = json.dumps(seen_inputs[-1], ensure_ascii=False)
    assert phrase not in final_input
    assert "[suppressed by owner memory directive]" in final_input
    assert "_context_suppression" not in final_input
