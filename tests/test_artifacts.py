from hashlib import sha256

from atlas.artifacts.models import ArtifactKind
from atlas.artifacts.store import ArtifactStore


def test_artifact_store_uses_identity_and_hash(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    payload = b"pony image bytes"

    artifact = store.put(
        payload,
        media_type="image/jpeg",
        source="test",
        kind=ArtifactKind.IMAGE,
        filename="pony.jpg",
    )

    stored = tmp_path / artifact.storage_key
    assert stored.read_bytes() == payload
    assert artifact.sha256 == sha256(payload).hexdigest()
    assert artifact.size_bytes == len(payload)
    assert artifact.filename == "pony.jpg"
