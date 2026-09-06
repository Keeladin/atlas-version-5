from sqlalchemy.exc import SQLAlchemyError

from atlas.config import Settings
from atlas.db import get_session_factory
from atlas.registry.repository import RegistryRepository
from atlas.registry.service import EnvironmentRegistry


async def initialize_phase0(
    settings: Settings,
    registry: EnvironmentRegistry,
) -> str | None:
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    factory = get_session_factory()
    try:
        async with factory() as session:
            repository = RegistryRepository(session)
            for entry in registry.all_entries():
                await repository.upsert(entry)
            await session.commit()
        return None
    except SQLAlchemyError as exc:
        return f"{type(exc).__name__}: {exc}"
