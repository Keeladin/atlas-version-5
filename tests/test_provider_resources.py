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
