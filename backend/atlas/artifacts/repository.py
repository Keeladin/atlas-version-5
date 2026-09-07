from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import ArtifactRow

from .models import Artifact


class ArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, artifact: Artifact, provenance: dict | None = None) -> Artifact:
        self.session.add(
            ArtifactRow(
                id=artifact.id,
                kind=artifact.kind.value,
                filename=artifact.filename,
                media_type=artifact.media_type,
                storage_key=artifact.storage_key,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                source=artifact.source,
                provenance=provenance or {},
                created_at=artifact.created_at,
            )
        )
        await self.session.flush()
        return artifact
