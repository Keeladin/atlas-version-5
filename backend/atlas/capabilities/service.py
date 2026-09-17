import asyncio
from collections.abc import Callable
from typing import Any

from jsonschema import FormatChecker
from jsonschema.validators import validator_for

from .models import (
    AuthorityMode,
    CapabilityCallResult,
    CapabilityFailure,
    OperationDescriptor,
)

Executor = Callable[[dict[str, Any]], Any]
ProposalSink = Callable[[OperationDescriptor, dict[str, Any]], Any]
AuthorityResolver = Callable[[dict[str, Any]], Any]  # returns AuthorityMode, or an awaitable of one

_NON_DELEGABLE_MANAGED_OPERATIONS = frozenset({
    "workspace.tasks.create",
    "workspace.tasks.cancel",
    "workspace.tasks.resume",
})


class CapabilityRuntime:
    def __init__(self, operations: list[OperationDescriptor] | None = None) -> None:
        self._operations: dict[str, OperationDescriptor] = {item.id: item for item in operations or []}
        self._executors: dict[str, Executor] = {}
        self._authority_resolvers: dict[str, AuthorityResolver] = {}
        self.policy_reader = None
        self.authority_reader = None  # async () -> {operation_id: authority}; the owner's per-operation decisions
        self.external_servers: list[dict[str, Any]] = []

    async def enabled_capabilities(self) -> set[str]:
        if self.policy_reader is None:
            return {item.capability_id for item in self._operations.values()} | {"openai.web"}
        return await self.policy_reader()

    async def compact_index_current(self):
        enabled = await self.enabled_capabilities()
        return [item for item in self.compact_index() if item["capability_id"] in enabled]

    async def authority_overrides(self) -> dict[str, AuthorityMode]:
        if self.authority_reader is None:
            return {}
        raw = await self.authority_reader()
        overrides: dict[str, AuthorityMode] = {}
        for operation_id, value in (raw or {}).items():
            try:
                overrides[str(operation_id)] = AuthorityMode(str(value))
            except ValueError:
                continue
        return overrides

    async def search_cards_current(self, query: str, limit: int = 8):
        enabled = await self.enabled_capabilities()
        overrides = await self.authority_overrides()
        cards = self.search_cards(query, limit, enabled=enabled)
        for card in cards:
            override = overrides.get(str(card["id"]))
            if override is not None:
                card["authority"] = override.value
        return cards

    async def descriptor_current(self, operation_id: str):
        item = self.descriptor(operation_id)
        if item is None or item.capability_id not in await self.enabled_capabilities():
            return None
        override = (await self.authority_overrides()).get(operation_id)
        return item.model_copy(update={"authority": override}) if override is not None else item

    async def preflight_authority_grants(self, operation_ids: list[str]) -> list[str]:
        """Validate exact task grants against current central Control policy before task creation."""
        enabled = await self.enabled_capabilities()
        overrides = await self.authority_overrides()
        normalized: list[str] = []
        problems: list[str] = []
        for raw in operation_ids[:64]:
            operation_id = str(raw or "").strip()
            if not operation_id or operation_id in normalized:
                continue
            if operation_id in _NON_DELEGABLE_MANAGED_OPERATIONS:
                problems.append(
                    f"{operation_id}: managed-task lifecycle authority cannot be delegated"
                )
                continue
            descriptor = self._operations.get(operation_id)
            if descriptor is None:
                problems.append(f"{operation_id}: operation is not registered")
                continue
            if descriptor.capability_id not in enabled:
                problems.append(f"{operation_id}: capability {descriptor.capability_id} is disabled")
                continue
            override = overrides.get(operation_id)
            hard_forbidden = override == AuthorityMode.FORBIDDEN or (
                override is None and descriptor.authority == AuthorityMode.FORBIDDEN
            )
            if hard_forbidden:
                problems.append(f"{operation_id}: denied by current Atlas Control policy")
                continue
            normalized.append(operation_id)
        if problems:
            raise ValueError("Managed-task authority preflight failed: " + "; ".join(problems))
        return normalized

    async def apply_task_grant(
        self,
        operation_id: str,
        resolved_authority: AuthorityMode,
    ) -> AuthorityMode:
        """Promote Ask me to Auto for an owner-approved task grant, never a hard Deny."""
        if operation_id in _NON_DELEGABLE_MANAGED_OPERATIONS:
            return resolved_authority
        if resolved_authority != AuthorityMode.APPROVAL_REQUIRED:
            return resolved_authority
        descriptor = self._operations.get(operation_id)
        if descriptor is None:
            return resolved_authority
        overrides = await self.authority_overrides()
        override = overrides.get(operation_id)
        if override == AuthorityMode.FORBIDDEN:
            return AuthorityMode.FORBIDDEN
        if override is None and descriptor.authority == AuthorityMode.FORBIDDEN:
            return AuthorityMode.FORBIDDEN
        return AuthorityMode.AUTO

    def register(self, descriptor: OperationDescriptor, executor: Executor, *,
            authority_resolver: AuthorityResolver | None = None) -> None:
        self._operations[descriptor.id] = descriptor
        self._executors[descriptor.id] = executor
        self._set_resolver(descriptor.id, authority_resolver)

    def register_executor(self, operation_id: str, executor: Executor, *,
            authority_resolver: AuthorityResolver | None = None) -> None:
        if operation_id not in self._operations:
            raise KeyError(f"Operation is not registered in the Environment Registry: {operation_id}")
        self._executors[operation_id] = executor
        self._set_resolver(operation_id, authority_resolver)

    def has_authority_resolver(self, operation_id: str) -> bool:
        return operation_id in self._authority_resolvers

    def _set_resolver(self, operation_id: str, resolver: AuthorityResolver | None) -> None:
        if resolver is None:
            self._authority_resolvers.pop(operation_id, None)
        else:
            self._authority_resolvers[operation_id] = resolver

    async def resolve_authority(self, operation_id: str, arguments: dict[str, Any]) -> AuthorityMode:
        """The owner's per-operation decision wins outright; otherwise the descriptor's default,
        refined per call by a configured resolver (which never relaxes a static FORBIDDEN)."""
        descriptor = self._operations.get(operation_id)
        if descriptor is None:
            raise KeyError(operation_id)
        override = (await self.authority_overrides()).get(operation_id)
        if override is not None:
            return override
        resolver = self._authority_resolvers.get(operation_id)
        if resolver is None or descriptor.authority == AuthorityMode.FORBIDDEN:
            return descriptor.authority
        resolved = resolver(arguments)
        if hasattr(resolved, "__await__"):
            resolved = await resolved
        return AuthorityMode(resolved)

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
        if not isinstance(arguments, dict):
            return "Capability arguments must be an object"
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
        validator_type = validator_for(schema)
        validator_type.check_schema(schema)
        error = next(validator_type(schema, format_checker=FormatChecker()).iter_errors(arguments), None)
        if error is not None:
            path = ".".join(str(item) for item in error.absolute_path) or "arguments"
            return f"Invalid {path}: schema rule {error.validator} failed"
        return None

    def search_cards(self, query: str, limit: int = 8, *, enabled: set[str] | None = None) -> list[dict[str, object]]:
        cards: list[dict[str, object]] = []
        for item in self.search(query, limit, enabled=enabled):
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

    def search(self, query: str, limit: int = 8, *, enabled: set[str] | None = None) -> list[OperationDescriptor]:
        terms = {term for term in query.casefold().replace("/", " ").replace(".", " ").split() if term}
        scored: list[tuple[int, OperationDescriptor]] = []
        for item in self.operations():
            if enabled is not None and item.capability_id not in enabled:
                continue
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
        authority: AuthorityMode | None = None,
    ) -> CapabilityCallResult:
        descriptor = self._operations.get(operation_id)
        executor = self._executors.get(operation_id)
        if descriptor is None or executor is None:
            return CapabilityCallResult(status="unavailable", operation_id=operation_id, message="Operation is not registered or enabled.")
        if descriptor.capability_id not in await self.enabled_capabilities():
            return CapabilityCallResult(status="forbidden", operation_id=operation_id,
                output={"failure_phase": "before_dispatch"}, message="The owner has not enabled this capability, or its current permission could not be verified.")
        if descriptor.authority == AuthorityMode.FORBIDDEN and authority is None and (
                await self.authority_overrides()).get(operation_id) is None:
            return CapabilityCallResult(status="forbidden", operation_id=operation_id, message="Operation is outside current Atlas authority.")
        validation_error = self.validate_arguments(operation_id, arguments)
        if validation_error:
            return CapabilityCallResult(status="failed", operation_id=operation_id,
                output={"failure_phase": "before_dispatch"}, message=validation_error)
        if authority is None:
            try:
                authority = await self.resolve_authority(operation_id, arguments)
            except Exception as exc:  # noqa: BLE001 - an unresolvable authority fails closed before dispatch
                return CapabilityCallResult(status="failed", operation_id=operation_id,
                    output={"failure_phase": "before_dispatch"}, message=f"Authority could not be resolved: {exc}")
        if authority == AuthorityMode.FORBIDDEN:
            return CapabilityCallResult(status="forbidden", operation_id=operation_id, message="Operation is outside current Atlas authority.")
        if authority == AuthorityMode.APPROVAL_REQUIRED and not approval_granted:
            if proposal_sink is None:
                return CapabilityCallResult(status="approval_required", operation_id=operation_id, message="Owner approval is required before execution.")
            proposal_id = await proposal_sink(descriptor, arguments)
            return CapabilityCallResult(status="approval_required", operation_id=operation_id, proposal_id=str(proposal_id), message="Action prepared and waiting for owner approval.")
        try:
            output = await asyncio.to_thread(executor, arguments)
            if hasattr(output, "__await__"):
                output = await output
        except CapabilityFailure as exc:
            return CapabilityCallResult(status="failed", operation_id=operation_id,
                output={**exc.output, "failure_phase": exc.phase}, message=str(exc))
        except Exception as exc:  # noqa: BLE001 - capability boundary must return tool failures
            return CapabilityCallResult(
                status="failed", operation_id=operation_id,
                output={"failure_phase": "ambiguous_dispatch"}, message=str(exc),
            )
        return CapabilityCallResult(status="succeeded", operation_id=operation_id, output=output)