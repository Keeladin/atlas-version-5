from atlas.capabilities import AuthorityMode, EffectKind, OperationDescriptor
from atlas.config import Settings

from .models import (
    CapabilityAvailability,
    CapabilityEntry,
    CapabilitySource,
)


class EnvironmentRegistry:
    def __init__(
        self,
        entries: list[CapabilityEntry] | None = None,
        operations: list[OperationDescriptor] | None = None,
    ) -> None:
        self._entries = {entry.id: entry for entry in entries or []}
        self._operations = {operation.id: operation for operation in operations or []}

    def upsert(self, entry: CapabilityEntry) -> None:
        self._entries[entry.id] = entry

    def register_operation(self, operation: OperationDescriptor) -> None:
        entry = self._entries.get(operation.capability_id)
        if entry is None or not entry.enabled or entry.availability != CapabilityAvailability.AVAILABLE:
            return
        self._operations[operation.id] = operation
        if operation.id not in entry.executable_operations:
            entry.executable_operations.append(operation.id)

    def all_entries(self) -> list[CapabilityEntry]:
        return sorted(self._entries.values(), key=lambda item: item.id)

    def enabled_projection(self) -> list[CapabilityEntry]:
        return [entry for entry in self.all_entries() if entry.enabled]

    def operations(self) -> list[OperationDescriptor]:
        return sorted(self._operations.values(), key=lambda item: item.id)


def build_phase0_registry(settings: Settings | None = None) -> EnvironmentRegistry:
    gws_ready = bool(settings and settings.gws_configured)
    entries = [
        CapabilityEntry(
            id="atlas.artifacts",
            family="Artifacts",
            description="Store and reference first-class conversation artifacts.",
            source=CapabilitySource.ATLAS,
            enabled=True,
            availability=CapabilityAvailability.AVAILABLE,
            executable_operations=[],
        ),
        CapabilityEntry(
            id="openai.web",
            family="Web",
            description="Search and read the public web using the active model provider's native web capability.",
            source=CapabilitySource.PROVIDER,
            enabled=bool(settings and settings.openai_api_key is not None),
            availability=(CapabilityAvailability.AVAILABLE if settings and settings.openai_api_key is not None else CapabilityAvailability.AUTHENTICATION_REQUIRED),
            executable_operations=[],
            trust="external",
        ),
        CapabilityEntry(
            id="atlas.local_storage",
            family="Storage",
            description="Browse the owner-approved local workspace root.",
            source=CapabilitySource.ATLAS,
            enabled=True,
            availability=CapabilityAvailability.AVAILABLE,
            executable_operations=["storage.local.list", "storage.local.acquire"],
        ),
        CapabilityEntry(
            id="github.mcp",
            family="GitHub",
            description="Access owner-authorized GitHub repositories through GitHub's official MCP server.",
            source=CapabilitySource.MCP,
            enabled=bool(settings and settings.github_configured),
            availability=(
                CapabilityAvailability.AVAILABLE
                if settings and settings.github_configured
                else CapabilityAvailability.AUTHENTICATION_REQUIRED
            ),
            executable_operations=[],
            trust="external",
        ),
        CapabilityEntry(
            id="google.workspace",
            family="Google Workspace",
            description="Access owner-authorized Google Workspace services through the Google Workspace MCP boundary.",
            source=CapabilitySource.MCP,
            enabled=gws_ready,
            availability=(
                CapabilityAvailability.AVAILABLE
                if gws_ready
                else CapabilityAvailability.AUTHENTICATION_REQUIRED
            ),
            executable_operations=["drive.files.list"] if gws_ready else [],
            trust="external",
        ),
    ]
    registry = EnvironmentRegistry(entries)
    registry.register_operation(OperationDescriptor(
        id="storage.local.list",
        capability_id="atlas.local_storage",
        family="Local storage",
        description="List files and folders inside the owner-approved local Atlas workspace.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the approved workspace root."}},
            "additionalProperties": False,
        },
        effect=EffectKind.READ,
        authority=AuthorityMode.AUTO,
    ))
    registry.register_operation(OperationDescriptor(
        id="storage.local.acquire",
        capability_id="atlas.local_storage",
        family="Local storage",
        description="Acquire one local workspace file as a model-readable resource so Atlas can perceive images and understand supported documents natively.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path relative to the approved workspace root."}},
            "required": ["path"],
            "additionalProperties": False,
        },
        effect=EffectKind.READ,
        authority=AuthorityMode.AUTO,
    ))
    if gws_ready:
        registry.register_operation(OperationDescriptor(
            id="drive.files.list",
            capability_id="google.workspace",
            family="Google Drive",
            description="List files and folders in an owner-authorized Google Drive folder.",
            input_schema={
                "type": "object",
                "properties": {"folder_id": {"type": "string", "description": "Google Drive folder ID; use root for My Drive."}},
                "additionalProperties": False,
            },
            effect=EffectKind.READ,
            authority=AuthorityMode.AUTO,
            trust="external",
        ))
    return registry
