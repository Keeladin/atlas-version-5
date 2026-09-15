import asyncio
import base64
import binascii
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI, OpenAIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from atlas.artifacts.models import ArtifactKind
from atlas.artifacts.service import ArtifactService
from atlas.artifacts.store import ArtifactStore
from atlas.capabilities.models import CapabilityFailure
from atlas.persistence.models import ArtifactRow

_MEDIA_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


class OpenAIImageService:
    """Provider image generation/editing with Atlas-native artifact persistence."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        factory: async_sessionmaker,
        store: ArtifactStore,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.factory = factory
        self.store = store
        self.client = client or AsyncOpenAI(api_key=api_key)

    @staticmethod
    def _options(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "size": str(arguments.get("size") or "auto"),
            "quality": str(arguments.get("quality") or "auto"),
            "background": str(arguments.get("background") or "auto"),
            "output_format": str(arguments.get("output_format") or "png"),
        }

    @staticmethod
    def _decode_response(response: Any) -> bytes:
        data = getattr(response, "data", None)
        item = data[0] if isinstance(data, list) and data else None
        encoded = getattr(item, "b64_json", None) if item is not None else None
        if not isinstance(encoded, str) or not encoded:
            raise CapabilityFailure("Image provider returned no image payload", phase="completed")
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CapabilityFailure("Image provider returned an invalid image payload", phase="completed") from exc

    async def _persist(
        self,
        data: bytes,
        *,
        output_format: str,
        filename: str,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        media_type = _MEDIA_TYPES[output_format]
        async with self.factory() as session:
            artifact = await ArtifactService(session, self.store).persist(
                data,
                media_type=media_type,
                source="openai_images",
                kind=ArtifactKind.IMAGE,
                filename=filename,
                provenance=provenance,
            )
        return {
            "artifact_id": str(artifact.id),
            "kind": artifact.kind.value,
            "filename": artifact.filename,
            "media_type": artifact.media_type,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "download_url": f"/api/artifacts/{artifact.id}",
        }

    async def generate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("An image prompt is required")
        options = self._options(arguments)
        try:
            response = await self.client.images.generate(
                model=self.model,
                prompt=prompt,
                n=1,
                **options,
            )
        except OpenAIError as exc:
            raise CapabilityFailure(f"OpenAI image generation failed: {exc}", phase="completed") from exc
        data = self._decode_response(response)
        output_format = options["output_format"]
        artifact = await self._persist(
            data,
            output_format=output_format,
            filename=f"atlas-generated.{output_format}",
            provenance={"provider": "openai", "model": self.model, "operation": "image.generate"},
        )
        return {
            "artifact": artifact,
            "provider": "openai",
            "model": self.model,
            "size": options["size"],
            "quality": options["quality"],
        }

    async def edit(self, arguments: dict[str, Any]) -> dict[str, Any]:
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("An image edit prompt is required")
        artifact_id = UUID(str(arguments.get("artifact_id") or ""))
        async with self.factory() as session:
            source = await session.get(ArtifactRow, artifact_id)
        if source is None:
            raise CapabilityFailure("Source artifact was not found", phase="before_dispatch")
        if not source.media_type.startswith("image/"):
            raise CapabilityFailure("Source artifact is not an image", phase="before_dispatch")
        try:
            source_data = await asyncio.to_thread(self.store.read, source.storage_key)
        except FileNotFoundError as exc:
            raise CapabilityFailure("Source artifact content is unavailable", phase="before_dispatch") from exc
        options = self._options(arguments)
        try:
            response = await self.client.images.edit(
                model=self.model,
                image=(source.filename or "source-image", source_data, source.media_type),
                prompt=prompt,
                n=1,
                input_fidelity=str(arguments.get("input_fidelity") or "high"),
                **options,
            )
        except OpenAIError as exc:
            raise CapabilityFailure(f"OpenAI image edit failed: {exc}", phase="completed") from exc
        data = self._decode_response(response)
        output_format = options["output_format"]
        artifact = await self._persist(
            data,
            output_format=output_format,
            filename=f"atlas-edited.{output_format}",
            provenance={"provider": "openai", "model": self.model,
                        "operation": "image.edit", "source_artifact_id": str(artifact_id)},
        )
        return {
            "artifact": artifact,
            "provider": "openai",
            "model": self.model,
            "source_artifact_id": str(artifact_id),
            "size": options["size"],
            "quality": options["quality"],
        }
