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
