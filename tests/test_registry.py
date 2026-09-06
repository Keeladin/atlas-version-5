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
