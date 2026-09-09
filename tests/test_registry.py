from atlas.registry.models import (
    CapabilityAvailability,
    CapabilityEntry,
    CapabilitySource,
)
from atlas.registry.service import EnvironmentRegistry


def test_disabled_capability_is_absent_from_agent_projection() -> None:
    registry = EnvironmentRegistry([
        CapabilityEntry(
            id="mail.send",
            family="Mail Send",
            description="Send mail",
            source=CapabilitySource.SERVICE,
            enabled=False,
            availability=CapabilityAvailability.AVAILABLE,
            executable_operations=["gmail.send_message"],
        )
    ])

    assert len(registry.all_entries()) == 1
    assert registry.enabled_projection() == []


def test_enabled_capability_is_visible() -> None:
    entry = CapabilityEntry(
        id="files.read",
        family="Filesystem",
        description="Read enabled files",
        source=CapabilitySource.LOCAL_SOFTWARE,
        enabled=True,
        availability=CapabilityAvailability.AVAILABLE,
    )
    assert EnvironmentRegistry([entry]).enabled_projection() == [entry]


def test_project_write_authority_keeps_targeted_edits_auto_and_delete_gated() -> None:
    from atlas.capabilities import AuthorityMode
    from atlas.registry.service import build_phase0_registry

    operations = {item.id: item for item in build_phase0_registry().operations()}
    assert operations["storage.projects.preview"].authority == AuthorityMode.AUTO
    assert operations["storage.projects.apply"].authority == AuthorityMode.AUTO
    assert operations["storage.projects.move"].authority == AuthorityMode.AUTO
    assert operations["storage.projects.delete"].authority == AuthorityMode.APPROVAL_REQUIRED


def test_memory_search_is_bounded_read_capability() -> None:
    from atlas.capabilities import AuthorityMode, EffectKind
    from atlas.registry.service import build_phase0_registry

    operation = {item.id: item for item in build_phase0_registry().operations()}["memory.search"]
    assert operation.effect == EffectKind.READ
    assert operation.authority == AuthorityMode.AUTO
    assert operation.input_schema["properties"]["limit"]["maximum"] == 10


def test_memory_registry_exposes_explicit_lifecycle_operations() -> None:
    from atlas.capabilities import AuthorityMode, EffectKind
    from atlas.registry.service import build_phase0_registry

    registry = build_phase0_registry()
    operations = {item.id: item for item in registry.operations()}
    assert "memory.forget" not in operations
    for name, effect in (("retire", EffectKind.UPDATE), ("restore", EffectKind.UPDATE),
                         ("delete", EffectKind.DELETE)):
        operation = operations[f"memory.{name}"]
        assert operation.effect == effect
        assert operation.authority == AuthorityMode.AUTO
    memory_entry = next(entry for entry in registry.all_entries() if entry.id == "atlas.memory")
    assert "memory.forget" not in memory_entry.executable_operations
    assert {"memory.retire", "memory.restore", "memory.delete"}.issubset(
        memory_entry.executable_operations
    )
