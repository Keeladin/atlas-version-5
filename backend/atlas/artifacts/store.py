import hashlib
from pathlib import Path
from uuid import uuid4

from .models import Artifact, ArtifactKind


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(
        self,
        data: bytes,
        *,
        media_type: str,
        source: str,
        kind: ArtifactKind = ArtifactKind.FILE,
        filename: str | None = None,
    ) -> Artifact:
        artifact_id = uuid4()
        storage_key = f"{artifact_id.hex[:2]}/{artifact_id}"
        destination = self.root / storage_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return Artifact(
            id=artifact_id,
            kind=kind,
            filename=filename,
            media_type=media_type,
            storage_key=storage_key,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            source=source,
        )

    def delete(self, storage_key: str) -> None:
        path = self.root / storage_key
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
