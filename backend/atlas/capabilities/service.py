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
        except Exception as exc:  # noqa: BLE001 - capability boundary must return tool failures
            return CapabilityCallResult(status="failed", operation_id=operation_id, message=str(exc))
        return CapabilityCallResult(status="succeeded", operation_id=operation_id, output=output)
