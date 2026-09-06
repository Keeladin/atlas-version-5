from .models import (
    CapabilityAvailability,
    CapabilityEntry,
    CapabilitySource,
)


class EnvironmentRegistry:
    def __init__(self, entries: list[CapabilityEntry] | None = None) -> None:
        self._entries = {entry.id: entry for entry in entries or []}

    def upsert(self, entry: CapabilityEntry) -> None:
        self._entries[entry.id] = entry

    def all_entries(self) -> list[CapabilityEntry]:
        return sorted(self._entries.values(), key=lambda item: item.id)

    def enabled_projection(self) -> list[CapabilityEntry]:
        return [entry for entry in self.all_entries() if entry.enabled]


def build_phase0_registry() -> EnvironmentRegistry:
    return EnvironmentRegistry([
        CapabilityEntry(
            id="atlas.artifacts",
            family="Artifacts",
            description="Store and reference first-class conversation artifacts.",
            source=CapabilitySource.ATLAS,
            enabled=True,
            availability=CapabilityAvailability.AVAILABLE,
            executable_operations=["artifact.store"],
        )
    ])
