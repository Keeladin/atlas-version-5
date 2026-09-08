"""Canonical observation snapshots and bounded reads of the exact stored evidence."""
import asyncio
import base64
import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select

from atlas.artifacts.models import ArtifactKind
from atlas.artifacts.repository import ArtifactRepository
from atlas.artifacts.store import ArtifactStore
from atlas.persistence.models import ArtifactRow, TranscriptRow, TurnRow
from atlas.runtime.task_state import record_runtime_event
from atlas.transcript.models import Actor, ToolObservationBlock
from atlas.transcript.repository import TranscriptRepository


class EvidenceStore:
    def __init__(self, session, artifacts: ArtifactStore):
        self.session = session
        self.artifacts = artifacts

    async def _blob(self, data: bytes, *, media_type: str, name: str | None, provenance: dict) -> dict:
        artifact = await asyncio.to_thread(self.artifacts.put, data, media_type=media_type,
            source=str(provenance.get('source') or 'runtime'), kind=ArtifactKind.TOOL_RESULT, filename=name)
        await ArtifactRepository(self.session).add(artifact, provenance)
        return {'artifact_id': str(artifact.id), 'snapshot_sha256': artifact.sha256,
            'snapshot_size_bytes': artifact.size_bytes}

    async def freeze(self, value: Any, *, provenance: dict) -> Any:
        if isinstance(value, list):
            return [await self.freeze(item, provenance=provenance) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: await self.freeze(item, provenance=provenance)
            for key, item in value.items() if key != 'data_base64'}
        if 'data_base64' in value:
            data = base64.b64decode(value['data_base64'], validate=True)
            existing = None
            if value.get('artifact_id'):
                try:
                    existing = await self.session.get(ArtifactRow, UUID(value['artifact_id']))
                except (ValueError, TypeError):
                    pass
            if existing is not None and hashlib.sha256(data).hexdigest() == existing.sha256:
                result.update({'artifact_id': str(existing.id), 'snapshot_sha256': existing.sha256,
                    'snapshot_size_bytes': existing.size_bytes})
            else:
                result.update(await self._blob(data, media_type=str(value.get('media_type') or 'application/octet-stream'),
                    name=value.get('name'), provenance={**provenance,
                        'resource': {k: v for k, v in result.items() if k != 'artifact_id'}}))
        return result

    async def record(self, transcript_id: UUID, *, operation: str, phase: str, detail: dict,
                     run_id: UUID | None = None, action_id: UUID | None = None,
                     arguments: dict | None = None, checkpoint: bool = True, trust: str = 'external') -> tuple[UUID, dict]:
        provenance = {'source': operation, 'trust': trust, 'run_id': str(run_id) if run_id else None,
            'transcript_id': str(transcript_id), 'action_id': str(action_id) if action_id else None}
        frozen = await self.freeze(detail, provenance=provenance)
        encoded = json.dumps(frozen, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8')
        stored = frozen
        artifact_id = None
        if len(encoded) > 32_000:
            reference = await self._blob(encoded, media_type='application/json', name='observation.json', provenance=provenance)
            artifact_id = UUID(reference['artifact_id'])
            stored = {'evidence_payload': reference, 'status': frozen.get('status'), 'operation_id': operation}
        repository = TranscriptRepository(self.session)
        turn = await repository.append_turn(transcript_id, Actor.TOOL, [ToolObservationBlock(
            action_id=action_id, artifact_id=artifact_id, operation=operation, phase=phase,
            summary=f'{operation} · {phase}', detail=stored, provenance=provenance)])
        if checkpoint:
            await repository.mutate_active_task_state(transcript_id, lambda state: record_runtime_event(
                state, operation=operation, phase=phase, evidence_id=str(turn.id),
                arguments=arguments, detail=frozen, action_id=str(action_id) if action_id else None))
        return turn.id, frozen

    async def _artifact_bytes(self, artifact_id: str) -> bytes:
        row = await self.session.get(ArtifactRow, UUID(artifact_id))
        if row is None:
            raise ValueError('Evidence artifact not found')
        path = (self.artifacts.root / row.storage_key).resolve(strict=True)
        if not path.is_relative_to(self.artifacts.root.resolve(strict=True)):
            raise ValueError('Invalid evidence storage reference')
        data = await asyncio.to_thread(path.read_bytes)
        if hashlib.sha256(data).hexdigest() != row.sha256:
            raise ValueError('Evidence artifact integrity check failed')
        return data

    async def read(self, evidence_id: str, *, pointer: str = '', offset: int = 0, limit: int = 4000,
                   artifact_id: str | None = None) -> dict:
        row = await self.session.get(TurnRow, UUID(evidence_id))
        if row is None:
            raise ValueError('Canonical evidence not found')
        blocks = row.blocks or []
        value: Any = blocks
        tool_block = (
            blocks[0]
            if len(blocks) == 1 and blocks[0].get('type') == 'tool_observation'
            else None
        )
        provenance: dict = {}
        operation = phase = action_id = None
        if tool_block is not None:
            evidence_kind = 'tool_observation'
            operation = tool_block.get('operation')
            phase = tool_block.get('phase')
            action_id = tool_block.get('action_id')
            provenance = tool_block.get('provenance') or {}
        elif row.actor == Actor.OWNER.value:
            evidence_kind = 'owner_attachment' if artifact_id is not None else 'owner_statement'
            if artifact_id is not None:
                reference = next((
                    block for block in blocks
                    if block.get('type') == 'artifact_ref'
                    and str(block.get('artifact_id')) == str(artifact_id)
                ), None)
                if reference is not None:
                    provenance = reference.get('provenance') or {}
        elif row.actor == Actor.ATLAS.value:
            evidence_kind = 'model_statement'
        elif row.actor == Actor.SYSTEM.value:
            evidence_kind = 'system_statement'
        else:
            evidence_kind = 'transcript_evidence'

        # Tool evidence has one observation block. Owner turns may contain
        # several durable attachment refs, selectable by JSON pointer.
        if tool_block is not None:
            value = tool_block['detail']
            if isinstance(value.get('evidence_payload'), dict):
                value = json.loads(await self._artifact_bytes(value['evidence_payload']['artifact_id']))
        if artifact_id is not None:
            def references(item):
                if isinstance(item, dict):
                    return item.get('artifact_id') == artifact_id or any(references(v) for v in item.values())
                if isinstance(item, list):
                    return any(references(v) for v in item)
                return False
            if not references(value):
                raise ValueError('Artifact is not referenced by this evidence')
            data = await self._artifact_bytes(artifact_id)
            try:
                value = data.decode('utf-8')
            except UnicodeDecodeError:
                raise ValueError('Binary evidence requires native resource acquisition') from None
        if pointer:
            if not pointer.startswith('/'):
                raise ValueError('Use an RFC 6901 JSON pointer')
            try:
                for part in pointer[1:].split('/'):
                    key = part.replace('~1', '/').replace('~0', '~')
                    value = value[int(key)] if isinstance(value, list) else value[key]
            except (KeyError, IndexError, ValueError, TypeError):
                raise ValueError('Evidence pointer does not exist') from None
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        offset = max(0, offset)
        limit = max(1, min(limit, 8000))
        end = min(len(text), offset + limit)
        return {
            'evidence_id': evidence_id,
            'transcript_id': str(row.transcript_id),
            'sequence': int(row.sequence),
            'actor': row.actor,
            'evidence_kind': evidence_kind,
            'created_at': row.created_at.isoformat() if row.created_at else None,
            'operation': operation,
            'phase': phase,
            'action_id': str(action_id) if action_id else None,
            'provenance': provenance,
            'artifact_id': artifact_id,
            'pointer': pointer,
            'offset': offset,
            'next_offset': end if end < len(text) else None,
            'total_characters': len(text),
            'text': text[offset:end],
            'exact': True,
            'trust': 'external',
        }

    async def task_read(self, task_id: str, *, expected_revision: int, offset: int = 0, limit: int = 4000) -> dict:
        row = (await self.session.execute(select(TranscriptRow).where(
            TranscriptRow.active_task_state["task_id"].astext == task_id))).scalar_one_or_none()
        if row is None:
            raise ValueError("Task checkpoint not found")
        if row.active_task_revision != expected_revision:
            raise ValueError(f"Task checkpoint changed; restart paging at revision {row.active_task_revision}")
        text = json.dumps(row.active_task_state, ensure_ascii=False, separators=(',', ':'))
        end = min(len(text), max(0, offset) + max(1, min(limit, 8000)))
        return {"task_id": task_id, "revision": expected_revision, "offset": offset,
            "text": text[offset:end], "next_offset": end if end < len(text) else None, "exact": True}

    async def acquire(self, evidence_id: str, artifact_id: str) -> dict:
        # Resolve the reference through the canonical observation first.
        row = await self.session.get(TurnRow, UUID(evidence_id))
        if row is None:
            raise ValueError('Canonical evidence not found')
        value = row.blocks
        for block in value:
            detail = block.get('detail', {})
            if isinstance(detail.get('evidence_payload'), dict):
                block = json.loads(await self._artifact_bytes(detail['evidence_payload']['artifact_id']))
            def linked(item):
                if isinstance(item, dict):
                    return item.get('artifact_id') == artifact_id or any(linked(v) for v in item.values())
                return isinstance(item, list) and any(linked(v) for v in item)
            if linked(block):
                artifact = await self.session.get(ArtifactRow, UUID(artifact_id))
                if artifact is None or artifact.size_bytes > 25 * 1024 * 1024:
                    raise ValueError('Evidence resource is unavailable or exceeds the acquisition limit')
                data = await self._artifact_bytes(artifact_id)
                return {'resource': {'artifact_id': artifact_id, 'name': artifact.filename or 'evidence',
                    'media_type': artifact.media_type, 'source': artifact.source,
                    'sha256': artifact.sha256, 'size_bytes': len(data),
                    'data_base64': base64.b64encode(data).decode('ascii')}}
        raise ValueError('Artifact is not referenced by this evidence')
