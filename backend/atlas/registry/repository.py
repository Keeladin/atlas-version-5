from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import RegistryEntryRow

from .models import CapabilityAvailability, CapabilityEntry, CapabilitySource


class RegistryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, entry: CapabilityEntry) -> None:
        values = {
            "id": entry.id,
            "family": entry.family,
            "description": entry.description,
            "source": entry.source.value,
            "provisioned": entry.provisioned,
            "enabled": entry.enabled,
            "availability": entry.availability.value,
            "metadata_json": {
                "executable_operations": entry.executable_operations,
                "trust": entry.trust,
            },
        }
        statement = insert(RegistryEntryRow).values(**values)
        statement = statement.on_conflict_do_update(index_elements=["id"], set_=values)
        await self.session.execute(statement)

    async def enabled_projection(self) -> list[CapabilityEntry]:
        result = await self.session.execute(
            select(RegistryEntryRow)
            .where(RegistryEntryRow.enabled.is_(True))
            .order_by(RegistryEntryRow.id)
        )
        entries = []
        for row in result.scalars():
            metadata = row.metadata_json or {}
            entries.append(
                CapabilityEntry(
                    id=row.id,
                    family=row.family,
                    description=row.description,
                    source=CapabilitySource(row.source),
                    provisioned=row.provisioned,
                    enabled=row.enabled,
                    availability=CapabilityAvailability(row.availability),
                    executable_operations=metadata.get("executable_operations", []),
                    trust=metadata.get("trust", "internal"),
                )
            )
        return entries
