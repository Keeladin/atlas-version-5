import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class _CapabilityBudget:
    limit: int = 16
    reserve: int = 2
    dispatched: int = 0
    operation_effects: dict[str, str] = field(default_factory=dict)

    def remember_search(self, result: dict[str, Any]) -> None:
        operations = result.get("operations")
        if not isinstance(operations, list):
            return
        for item in operations:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                self.operation_effects[item["id"]] = str(item.get("effect") or "")

    def can_dispatch(self, operation_id: str) -> tuple[bool, str | None]:
        if self.dispatched >= self.limit:
            return False, "Atlas reached the dispatched capability-call limit for this turn."
        reserve_start = max(0, self.limit - self.reserve)
        effect = self.operation_effects.get(operation_id)
        completion_step = effect not in {None, "", "read"} or operation_id.endswith(".preview")
        if self.dispatched >= reserve_start and not completion_step:
            return False, (
                f"Atlas is reserving the final {self.reserve} capability calls for completion steps. "
                "Stop exploratory reads and finish the task with preview/apply or another effecting operation."
            )
        return True, None

    def record_dispatch(self) -> None:
        self.dispatched += 1

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.dispatched)



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
        return {"role": "user", "content": content}

    # Code, config, logs, extensionless text and other UTF-8 resources should be
    # supplied as text rather than an input_file with a MIME type the provider
    # may reject (notably application/octet-stream and many text/* subtypes).
    try:
        raw = base64.b64decode(data, validate=True)
        decoded = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        decoded = None
    if decoded is not None:
        return {
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": f"Atlas runtime acquired {name} from {source}. File contents follow:\n\n{decoded}",
            }],
        }

    if media_type == "application/octet-stream":
        return {
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": (
                    f"Atlas runtime acquired binary resource {name} from {source}, but its format "
                    "could not be identified safely for native provider file input."
                ),
            }],
        }

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
    def __init__(
        self, *, api_key: str, model: str, capability_call_limit: int = 16, capability_completion_reserve: int = 2
    ) -> None:
        self.model = model
        self.capability_call_limit = max(1, capability_call_limit)
        self.capability_completion_reserve = max(0, min(capability_completion_reserve, self.capability_call_limit))
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

    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        response = await self.client.responses.create(
            model=self.model,
            instructions=instructions,
            input=messages,
            store=False,
        )
        return response.output_text or ""

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
        budget = _CapabilityBudget(limit=self.capability_call_limit, reserve=self.capability_completion_reserve)
        # Discovery is intentionally outside the dispatched-operation budget. The separate
        # reasoning-round ceiling still prevents a model from searching forever.
        for _ in range(max(24, self.capability_call_limit + 8)):
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
                if call.name == "atlas_capability_call":
                    operation_id = str(arguments.get("operation_id") or "")
                    allowed, reason = budget.can_dispatch(operation_id)
                    if not allowed:
                        result = {"status": "budget_reserved", "message": reason, "remaining_calls": budget.remaining}
                    else:
                        result = await tool_handler(call.name, arguments)
                        budget.record_dispatch()
                else:
                    result = await tool_handler(call.name, arguments)
                    if call.name == "atlas_capability_search":
                        budget.remember_search(result)
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
        yield "I reached the bounded tool-reasoning limit for this turn before finishing the task."
