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
