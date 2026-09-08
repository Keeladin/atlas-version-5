import os
from uuid import uuid4

import pytest
from atlas.artifacts.store import ArtifactStore
from atlas.config import Settings
from atlas.maintenance.backup import backup, restore
from atlas.persistence.models import ArtifactRow, Base, TranscriptRow
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def test_offline_backup_restore_preserves_database_and_exact_artifacts(tmp_path):
    database_url = os.environ.get('ATLAS_TEST_DATABASE_URL')
    if not database_url:
        pytest.skip('Set ATLAS_TEST_DATABASE_URL to a disposable PostgreSQL database')
    admin_url = make_url(database_url)
    admin = create_engine(admin_url, isolation_level='AUTOCOMMIT')
    source_name, target_name = 'backup_source_' + uuid4().hex, 'backup_target_' + uuid4().hex
    source_url, target_url = admin_url.set(database=source_name), admin_url.set(database=target_name)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE {source_name}'))
        connection.execute(text(f'CREATE DATABASE {target_name}'))
    source_engine = create_engine(source_url)
    target_engine = create_engine(target_url)
    try:
        with source_engine.begin() as connection:
            connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        Base.metadata.create_all(source_engine)
        source = Settings(database_url=source_url.render_as_string(hide_password=False), database_url_file=None,
            artifact_dir=tmp_path / 'source-artifacts', project_checkpoint_root=tmp_path / 'source-checkpoints',
            auth_enrolled_marker_file=tmp_path / 'source-auth' / 'enrolled')
        artifact = ArtifactStore(source.artifact_dir).put(b'Exact artifact evidence\x00\xff', media_type='application/octet-stream', source='fixture')
        source.project_checkpoint_root.mkdir()
        (source.project_checkpoint_root / 'change.zip').write_bytes(b'Fixture pending change bundle')
        source.auth_enrolled_marker_file.parent.mkdir()
        source.auth_enrolled_marker_file.write_text('enrolled')
        with Session(source_engine) as session:
            row = TranscriptRow(kind='owner', active_task_state={'task_id': str(uuid4()), 'status': 'active', 'semantic': {'objective': 'Keep this'}})
            session.add(row)
            session.add(ArtifactRow(id=artifact.id, kind=artifact.kind.value, filename=artifact.filename, media_type=artifact.media_type,
                storage_key=artifact.storage_key, sha256=artifact.sha256, size_bytes=artifact.size_bytes, source=artifact.source, provenance={'fixture': True}))
            session.commit()
            transcript_id = row.id
        destination = tmp_path / 'backup'
        assert backup(source, destination)['status'] == 'complete'
        restored = Settings(database_url=target_url.render_as_string(hide_password=False), database_url_file=None,
            artifact_dir=tmp_path / 'restored-artifacts', project_checkpoint_root=tmp_path / 'restored-checkpoints',
            auth_enrolled_marker_file=tmp_path / 'restored-auth' / 'enrolled')
        assert restore(restored, destination)['status'] == 'restored'
        with Session(target_engine) as session:
            assert session.get(TranscriptRow, transcript_id).active_task_state['semantic']['objective'] == 'Keep this'
            assert session.get(ArtifactRow, artifact.id).sha256 == artifact.sha256
        assert (restored.artifact_dir / artifact.storage_key).read_bytes() == b'Exact artifact evidence\x00\xff'
        assert (restored.project_checkpoint_root / 'change.zip').read_bytes() == b'Fixture pending change bundle'
        assert restored.auth_enrolled_marker_file.read_text() == 'enrolled'
        with pytest.raises(ValueError, match='empty'):
            restore(restored, destination)
        (destination / 'artifacts' / artifact.storage_key).write_bytes(b'Corrupted')
        with pytest.raises(ValueError, match='integrity'):
            restore(restored, destination)
    finally:
        source_engine.dispose()
        target_engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE {source_name} WITH (FORCE)'))
            connection.execute(text(f'DROP DATABASE {target_name} WITH (FORCE)'))
        admin.dispose()
