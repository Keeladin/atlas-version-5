import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]



def _resource_input(resource: dict[str, Any]) -> dict[str, Any] | None:
    data = resource.get("data_base64")
    media_type = str(resource.get("media_type") or "application/octet-stream")
    name = str(resource.get("name") or "resource")
    source = str(resource.get("source") or "Atlas runtime")
    if not isinstance(data, str) or not data:
        return None
    if media_type.startswith("image/"):
        content = [
            {"type": "input_text", "text": f"Atlas runtime acquired {name} from {source}. Inspect the image itself when answering the owner's request."},
            {"type": "input_image", "detail": "auto", "image_url": f"data:{media_type};base64,{data}"},
        ]
    else:
        content = [
            {"type": "input_text", "text": f"Atlas runtime acquired {name} from {source}. Use the file contents when answering the owner's request."},
            {"type": "input_file", "filename": name, "file_data": f"data:{media_type};base64,{data}"},
        ]
    return {"role": "user", "content": content}


def _public_tool_result(result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    resource = None
    public = dict(result)
    output = result.get("output")
    if isinstance(output, dict) and isinstance(output.get("resource"), dict):
        resource = output["resource"]
        public_output = dict(output)
        public_resource = {key: value for key, value in resource.items() if key != "data_base64"}
        public_output["resource"] = public_resource
        public["output"] = public_output
    return public, resource

_MODEL_TOOLS = [
    {"type": "web_search"},
    {
        "type": "function",
        "name": "atlas_capability_search",
        "description": "Search Atlas's enabled capability registry for operations relevant to the current task. Use this instead of guessing tool names.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What capability or operation is needed."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "atlas_capability_call",
        "description": "Invoke one operation returned by atlas_capability_search. Atlas runtime enforces enablement and owner authority before execution.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation_id": {"type": "string"},
                "arguments": {"type": "object", "additionalProperties": True},
            },
            "required": ["operation_id", "arguments"],
            "additionalProperties": False,
        },
        "strict": False,
    },
]


class OpenAIProvider:
    def __init__(self, *, api_key: str, model: str) -> None:
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key)

    async def count_input_tokens(
        self,
        *,
        instructions: str,
        messages: list[dict[str, str]],
    ) -> int:
        result = await self.client.responses.input_tokens.count(
            model=self.model,
            instructions=instructions,
            input=messages,
            tools=_MODEL_TOOLS,
        )
        return result.input_tokens

    async def stream_text(
        self,
        *,
        instructions: str,
        messages: list[dict[str, str]],
        tool_handler: ToolHandler | None = None,
    ) -> AsyncIterator[str]:
        if tool_handler is None:
            stream = await self.client.responses.create(
                model=self.model,
                instructions=instructions,
                input=messages,
                tools=[{"type": "web_search"}],
                tool_choice="auto",
                stream=True,
                store=False,
            )
            async for event in stream:
                if event.type == "response.output_text.delta":
                    yield event.delta
            return

        input_items: list[Any] = list(messages)
        for _ in range(8):
            response = await self.client.responses.create(
                model=self.model,
                instructions=instructions,
                input=input_items,
                tools=_MODEL_TOOLS,
                tool_choice="auto",
                store=False,
            )
            calls = [item for item in response.output if getattr(item, "type", None) == "function_call"]
            if not calls:
                if response.output_text:
                    yield response.output_text
                return
            input_items.extend(item.model_dump(exclude_none=True) for item in response.output)
            for call in calls:
                try:
                    arguments = json.loads(call.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                result = await tool_handler(call.name, arguments)
                public_result, resource = _public_tool_result(result)
                input_items.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(public_result, ensure_ascii=False, default=str),
                })
                if resource is not None:
                    resource_item = _resource_input(resource)
                    if resource_item is not None:
                        input_items.append(resource_item)
        yield "I reached the capability-call limit for this turn before finishing the task."
