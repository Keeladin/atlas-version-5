import base64
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from atlas.artifacts.models import ArtifactKind
from atlas.artifacts.service import ArtifactService
from atlas.artifacts.store import ArtifactStore
from atlas.capabilities.models import AuthorityMode, CapabilityCallResult, EffectKind
from atlas.config import Settings
from atlas.integrations.openai_images import OpenAIImageService
from atlas.persistence.models import ArtifactRow
from atlas.registry.service import build_phase0_registry
from atlas.runtime.execution import RunExecutor


class FakeImages:
    def __init__(self) -> None:
        self.generate_calls = []
        self.edit_calls = []

    async def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        encoded = base64.b64encode(b"generated-png").decode()
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        encoded = base64.b64encode(b"edited-png").decode()
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])

class FakeClient:
    def __init__(self) -> None:
        self.images = FakeImages()


@pytest.mark.asyncio
async def test_generate_persists_image_artifact_without_returning_base64(pg_factory, tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    client = FakeClient()
    service = OpenAIImageService(
        api_key="test",
        model="gpt-image-2.5-sunburst",
        factory=pg_factory,
        store=store,
        client=client,
    )

    result = await service.generate({"prompt": "A blue mining helmet", "size": "1024x1024"})

    assert result["artifact"]["media_type"] == "image/png"
    assert "b64" not in json.dumps(result)
    call = client.images.generate_calls[-1]
    assert call["model"] == "gpt-image-2.5-sunburst"
    assert call["prompt"] == "A blue mining helmet"
    assert "response_format" not in call

    artifact_id = result["artifact"]["artifact_id"]
    async with pg_factory() as session:
        row = await session.get(ArtifactRow, artifact_id)
        assert row is not None
        assert store.read(row.storage_key) == b"generated-png"

@pytest.mark.asyncio
async def test_edit_uses_exact_existing_image_artifact(pg_factory, tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    async with pg_factory() as session:
        source = await ArtifactService(session, store).persist(
            b"source-png",
            media_type="image/png",
            source="test",
            kind=ArtifactKind.IMAGE,
            filename="reference.png",
        )
    client = FakeClient()
    service = OpenAIImageService(
        api_key="test",
        model="gpt-image-2.5-sunburst",
        factory=pg_factory,
        store=store,
        client=client,
    )

    result = await service.edit({
        "artifact_id": str(source.id),
        "prompt": "Keep the person, change the background to dusk",
        "input_fidelity": "high",
    })

    call = client.images.edit_calls[-1]
    assert call["image"] == ("reference.png", b"source-png", "image/png")
    assert call["input_fidelity"] == "high"
    assert result["source_artifact_id"] == str(source.id)
    assert result["artifact"]["media_type"] == "image/png"

def test_image_operations_are_first_class_auto_create_capabilities(tmp_path):
    key = tmp_path / "openai-key"
    key.write_text("test-key")
    registry = build_phase0_registry(Settings(openai_api_key_file=key))
    entry = next(item for item in registry.all_entries() if item.id == "openai.images")
    assert entry.availability.value == "available"
    assert entry.source.value == "provider"

    operations = {item.id: item for item in registry.operations()}
    for operation_id in ("image.generate", "image.edit"):
        operation = operations[operation_id]
        assert operation.capability_id == "openai.images"
        assert operation.effect == EffectKind.CREATE
        assert operation.authority == AuthorityMode.AUTO
        assert operation.trust == "external"


def test_run_executor_collects_successful_image_artifacts_once():
    executor = RunExecutor(None, None, None, run_id=uuid4(), transcript_id=uuid4())
    result = CapabilityCallResult(
        status="succeeded",
        operation_id="image.generate",
        output={"artifact": {
            "artifact_id": str(uuid4()),
            "kind": "image",
            "filename": "atlas-generated.png",
            "media_type": "image/png",
        }},
    )
    executor.remember_output_artifact(result)
    executor.remember_output_artifact(result)
    assert len(executor.output_artifacts) == 1
    assert executor.output_artifacts[0]["operation"] == "image.generate"


def test_artifact_store_rejects_storage_keys_outside_root(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ValueError, match="escapes"):
        store.path_for("../../outside")

@pytest.mark.asyncio
async def test_edit_missing_artifact_fails_before_provider_dispatch(pg_factory, tmp_path):
    from atlas.capabilities.models import CapabilityFailure

    client = FakeClient()
    service = OpenAIImageService(
        api_key="test",
        model="gpt-image-2.5-sunburst",
        factory=pg_factory,
        store=ArtifactStore(tmp_path / "artifacts"),
        client=client,
    )
    with pytest.raises(CapabilityFailure) as error:
        await service.edit({"artifact_id": str(uuid4()), "prompt": "Change the background"})
    assert error.value.phase == "before_dispatch"
    assert client.images.edit_calls == []
