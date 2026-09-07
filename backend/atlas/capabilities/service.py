from collections.abc import Callable
from typing import Any

from .models import AuthorityMode, CapabilityCallResult, OperationDescriptor

Executor = Callable[[dict[str, Any]], Any]
ProposalSink = Callable[[OperationDescriptor, dict[str, Any]], Any]


class CapabilityRuntime:
    def __init__(self, operations: list[OperationDescriptor] | None = None) -> None:
        self._operations: dict[str, OperationDescriptor] = {item.id: item for item in operations or []}
        self._executors: dict[str, Executor] = {}

    def register(self, descriptor: OperationDescriptor, executor: Executor) -> None:
        self._operations[descriptor.id] = descriptor
        self._executors[descriptor.id] = executor

    def register_executor(self, operation_id: str, executor: Executor) -> None:
        if operation_id not in self._operations:
            raise KeyError(f"Operation is not registered in the Environment Registry: {operation_id}")
        self._executors[operation_id] = executor

    def operations(self) -> list[OperationDescriptor]:
        return sorted(self._operations.values(), key=lambda item: item.id)

    def compact_index(self) -> list[dict[str, str]]:
        seen: dict[str, str] = {}
        for item in self.operations():
            seen.setdefault(item.family, item.capability_id)
        return [{"family": family, "capability_id": capability_id} for family, capability_id in seen.items()]

    def descriptor(self, operation_id: str) -> OperationDescriptor | None:
        return self._operations.get(operation_id)

    def validate_arguments(self, operation_id: str, arguments: dict[str, Any]) -> str | None:
        descriptor = self._operations.get(operation_id)
        if descriptor is None:
            return "Operation is not registered or enabled."
        schema = descriptor.input_schema or {}
        required = schema.get("required")
        if isinstance(required, list):
            missing = [str(key) for key in required if key not in arguments]
            if missing:
                return f"Missing required argument(s): {', '.join(missing)}"
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            empty_strings = [
                str(key) for key in required
                if isinstance(properties.get(key), dict)
                and properties[key].get("type") == "string"
                and isinstance(arguments.get(key), str)
                and not arguments[key].strip()
            ]
            if empty_strings:
                return f"Required argument(s) cannot be empty: {', '.join(empty_strings)}"
        if schema.get("additionalProperties") is False:
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            extras = sorted(set(arguments) - set(properties))
            if extras:
                return f"Unexpected argument(s): {', '.join(extras)}"
        return None

    def search_cards(self, query: str, limit: int = 8) -> list[dict[str, object]]:
        cards: list[dict[str, object]] = []
        for item in self.search(query, limit):
            schema = item.input_schema or {}
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            cards.append({
                "id": item.id,
                "family": item.family,
                "description": item.description,
                "effect": item.effect.value,
                "authority": item.authority.value,
                "arguments": {
                    key: str(value.get("type") or "any") if isinstance(value, dict) else "any"
                    for key, value in properties.items()
                },
                "required": [str(key) for key in required],
            })
        return cards

    def search(self, query: str, limit: int = 8) -> list[OperationDescriptor]:
        terms = {term for term in query.casefold().replace("/", " ").replace(".", " ").split() if term}
        scored: list[tuple[int, OperationDescriptor]] = []
        for item in self.operations():
            haystack = f"{item.id} {item.family} {item.description}".casefold()
            score = sum(3 if term in item.id.casefold() else 1 for term in terms if term in haystack)
            if score or not terms:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [item for _, item in scored[: max(1, min(limit, 20))]]

    async def call(
        self,
        operation_id: str,
        arguments: dict[str, Any],
        *,
        proposal_sink: ProposalSink | None = None,
        approval_granted: bool = False,
    ) -> CapabilityCallResult:
        descriptor = self._operations.get(operation_id)
        executor = self._executors.get(operation_id)
        if descriptor is None or executor is None:
            return CapabilityCallResult(status="unavailable", operation_id=operation_id, message="Operation is not registered or enabled.")
        if descriptor.authority == AuthorityMode.FORBIDDEN:
            return CapabilityCallResult(status="forbidden", operation_id=operation_id, message="Operation is outside current Atlas authority.")
        if descriptor.authority == AuthorityMode.APPROVAL_REQUIRED and not approval_granted:
            if proposal_sink is None:
                return CapabilityCallResult(status="approval_required", operation_id=operation_id, message="Owner approval is required before execution.")
            proposal_id = await proposal_sink(descriptor, arguments)
            return CapabilityCallResult(status="approval_required", operation_id=operation_id, proposal_id=str(proposal_id), message="Action prepared and waiting for owner approval.")
        try:
            output = executor(arguments)
            if hasattr(output, "__await__"):
                output = await output
        except ValueError as exc:
            return CapabilityCallResult(
                status="failed", operation_id=operation_id,
                output={"failure_phase": "before_dispatch"}, message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - capability boundary must return tool failures
            return CapabilityCallResult(
                status="failed", operation_id=operation_id,
                output={"failure_phase": "ambiguous_dispatch"}, message=str(exc),
            )
        return CapabilityCallResult(status="succeeded", operation_id=operation_id, output=output)
