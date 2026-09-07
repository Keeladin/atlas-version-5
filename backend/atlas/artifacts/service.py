from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Artifact, ArtifactKind
from .repository import ArtifactRepository
from .store import ArtifactStore


class ArtifactService:
    def __init__(self, session: AsyncSession, store: ArtifactStore) -> None:
        self.session = session
        self.store = store
        self.repository = ArtifactRepository(session)

    async def persist(
        self,
        data: bytes,
        *,
        media_type: str,
        source: str,
        kind: ArtifactKind,
        filename: str | None = None,
        provenance: dict | None = None,
    ) -> Artifact:
        artifact = self.store.put(
            data,
            media_type=media_type,
            source=source,
            kind=kind,
            filename=filename,
        )
        try:
            await self.repository.add(artifact, provenance)
            await self.session.commit()
        except SQLAlchemyError:
            await self.session.rollback()
            self.store.delete(artifact.storage_key)
            raise
        return artifact
