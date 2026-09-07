"""Offline PostgreSQL + immutable-file backup and restore into EMPTY targets."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from atlas.config import Settings


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def pg_environment(url):
    parsed = make_url(url)
    env = {**os.environ, 'PGDATABASE': parsed.database or 'postgres', 'PGUSER': parsed.username or '',
        'PGHOST': str(parsed.query.get('host') or parsed.host or ''),
        'PGPORT': str(parsed.query.get('port') or parsed.port or 5432)}
    env.pop('PGOPTIONS', None)
    if parsed.password is not None:
        env['PGPASSWORD'] = parsed.password
    else:
        env.pop('PGPASSWORD', None)
    for key in ('sslmode', 'sslrootcert', 'sslcert', 'sslkey'):
        if key in parsed.query:
            env['PG' + key.upper()] = str(parsed.query[key])
    return env


def pg_command(command, url):
    completed = subprocess.run(command, env=pg_environment(url), capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f'{command[0]} failed; no successful backup/restore was recorded')


def copy_tree(source, target):
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.is_symlink():
        raise ValueError("State roots cannot be symlinks")
    if not source.exists():
        return
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('State snapshots cannot contain symlinks')
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(mode=0o700, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open('rb') as original, destination.open('xb') as output:
                shutil.copyfileobj(original, output)
                output.flush()
                os.fsync(output.fileno())
            destination.chmod(0o600)
        else:
            raise ValueError('Only regular files and directories can be backed up')


def sync_tree(root):
    for path in root.rglob('*'):
        if path.is_file():
            with path.open('rb') as handle:
                os.fsync(handle.fileno())
    directories = [root, *(path for path in root.rglob('*') if path.is_dir())]
    for path in reversed(directories):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def verify_artifacts(url, artifacts):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text('SELECT storage_key, sha256, size_bytes FROM artifacts'))
            for storage_key, sha256, size in rows:
                path = artifacts / storage_key
                if path.is_symlink() or not path.resolve().is_relative_to(artifacts.resolve()) or not path.is_file():
                    raise ValueError('A canonical artifact is missing or escapes the artifact root')
                if path.stat().st_size != size or digest(path) != sha256:
                    raise ValueError('Canonical artifact content does not match database evidence')
    finally:
        engine.dispose()


def backup(settings: Settings, destination: Path):
    destination = destination.resolve()
    components = {'artifacts': settings.artifact_dir, 'project-checkpoints': settings.project_checkpoint_root}
    if any(destination.is_relative_to(path.resolve()) or path.resolve().is_relative_to(destination) for path in components.values()):
        raise ValueError('Backup output must be separate from live state')
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    # COMPLETE is written last. A failed/partial directory is never restorable.
    dump = destination / 'database.dump'
    pg_command(['pg_dump', '--format=custom', '--no-owner', '--no-acl', '--file', str(dump)], settings.database_dsn)
    dump.chmod(0o600)
    for name, source in components.items():
        copy_tree(source, destination / name)
    marker = settings.auth_enrolled_marker_file
    if marker.is_file() and not marker.is_symlink():
        (destination / 'owner-enrolled').write_bytes(marker.read_bytes())
        (destination / 'owner-enrolled').chmod(0o600)
    verify_artifacts(settings.database_dsn, destination / 'artifacts')
    files = {path.relative_to(destination).as_posix(): digest(path) for path in destination.rglob('*') if path.is_file()}
    manifest = {'version': 1, 'created_at': datetime.now(UTC).isoformat(), 'files': files,
        'scope': 'PostgreSQL, artifacts, project checkpoints/change bundles and owner enrollment marker. Provider credentials and runtime configuration require a separate protected backup.'}
    manifest_path = destination / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2))
    # Persist every file and directory before publishing the completion marker.
    sync_tree(destination)
    with (destination / 'COMPLETE').open('x') as output:
        output.write(digest(manifest_path))
        output.flush()
        os.fsync(output.fileno())
    sync_tree(destination)
    return {'status': 'complete', 'files': len(files)}


def restore(settings: Settings, source: Path):
    source = source.resolve()
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in source.rglob('*')):
        raise ValueError('Backup contains symlinks or non-regular files')
    if not (source / 'COMPLETE').is_file() or (source / 'COMPLETE').read_text().strip() != digest(source / 'manifest.json'):
        raise ValueError('Backup is incomplete or its manifest changed')
    manifest = json.loads((source / 'manifest.json').read_text())
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict) or 'database.dump' not in manifest['files']:
        raise ValueError('Unsupported backup version')
    for name, expected in manifest['files'].items():
        path = source / name
        if path.is_symlink() or not path.resolve().is_relative_to(source) or not path.is_file() or digest(path) != expected:
            raise ValueError('Backup file integrity check failed')
    actual = {path.relative_to(source).as_posix() for path in source.rglob('*') if path.is_file()} - {'COMPLETE', 'manifest.json'}
    if actual != set(manifest['files']):
        raise ValueError('Backup contains missing or unlisted files')
    targets = {'artifacts': settings.artifact_dir, 'project-checkpoints': settings.project_checkpoint_root}
    for target in targets.values():
        if target.is_symlink() or target.resolve().is_relative_to(source) or source.is_relative_to(target.resolve()) or (target.exists() and any(target.iterdir())):
            raise ValueError('Restore requires empty, separate state directories')
    target_roots = [path.resolve() for path in targets.values()]
    if target_roots[0].is_relative_to(target_roots[1]) or target_roots[1].is_relative_to(target_roots[0]):
        raise ValueError('Restore state directories must not overlap')
    marker = settings.auth_enrolled_marker_file
    if marker.resolve().is_relative_to(source) or any(marker.resolve().is_relative_to(path) for path in target_roots):
        raise ValueError('Restore marker must be separate from snapshot and state directories')
    if marker.exists() or marker.is_symlink():
        raise ValueError('Restore requires a new owner enrollment marker location')
    engine = create_engine(settings.database_dsn)
    try:
        with engine.connect() as connection:
            count = connection.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')")).scalar_one()
            if count:
                raise ValueError('Restore requires an empty database; existing state is never overwritten')
    finally:
        engine.dispose()
    pg_command(['pg_restore', '--exit-on-error', '--single-transaction', '--no-owner', '--no-acl', '--dbname', make_url(settings.database_dsn).database,
        str(source / 'database.dump')], settings.database_dsn)
    for name, target in targets.items():
        copy_tree(source / name, target)
    verify_artifacts(settings.database_dsn, settings.artifact_dir)
    if (source / 'owner-enrolled').is_file():
        settings.auth_enrolled_marker_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with settings.auth_enrolled_marker_file.open('xb') as output:
            output.write((source / 'owner-enrolled').read_bytes())
        settings.auth_enrolled_marker_file.chmod(0o600)
        sync_tree(settings.auth_enrolled_marker_file.parent)
    for target in targets.values():
        sync_tree(target)
    return {'status': 'restored', 'runtime_started': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['backup', 'restore'])
    parser.add_argument('directory', type=Path)
    parser.add_argument('--offline', action='store_true', help='Required: all Atlas processes must already be stopped')
    parser.add_argument('--database-url-file', type=Path)
    parser.add_argument('--artifact-dir', type=Path)
    parser.add_argument('--checkpoint-dir', type=Path)
    parser.add_argument('--enrolled-marker', type=Path)
    args = parser.parse_args()
    if not args.offline:
        parser.error('Stop Atlas first and pass --offline; snapshots require a quiescent runtime')
    overrides = {name: value for name, value in {'database_url_file': args.database_url_file,
        'artifact_dir': args.artifact_dir, 'project_checkpoint_root': args.checkpoint_dir,
        'auth_enrolled_marker_file': args.enrolled_marker}.items() if value is not None}
    settings = Settings(**overrides)
    try:
        result = (backup if args.operation == 'backup' else restore)(settings, args.directory)
    except (SQLAlchemyError, OSError, ValueError, RuntimeError, KeyError, TypeError):
        print('Backup/restore failed. Keep Atlas stopped; verify snapshot integrity, empty targets, permissions and PostgreSQL access before retrying.', file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
