"""Prepare reviewable project changes without writing into owner working files."""
import hashlib
import io
import json
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4


class ProjectChanges:
    def __init__(self, projects):
        self.projects = projects
        if projects.checkpoint_root is None:
            raise ValueError('Project change storage is not configured')
        self.root = projects.checkpoint_root / 'changes'

    def _snapshot(self, path):
        self.projects._assert_readable(path)
        _, data, info = self.projects._read_checked(self.projects.root.resolve() / path, self.projects._MAX_CHECKPOINT_BYTES)
        return data, info.st_mode & 0o777

    def _save(self, operation, path, expected_sha256, before, after=None, target_path=None, mode=0o644):
        change_id = uuid4()
        manifest = {'version': 1, 'change_id': str(change_id), 'operation': operation, 'path': path,
            'target_path': target_path, 'expected_sha256': expected_sha256, 'mode': oct(mode),
            'created_at': datetime.now(UTC).isoformat(), 'live_files_modified': False,
            'integration': 'Review the proposed change and integrate it through your editor or version-control workflow. Recheck the live file; the baseline may have changed.'}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr('manifest.json', json.dumps(manifest, indent=2))
            bundle.writestr('README.txt', manifest['integration'] + '\nOriginal and proposed bytes are included for a three-way comparison. No automatic apply script is included.\n')
            if before is not None:
                bundle.writestr('original', before)
            if after is not None:
                bundle.writestr('proposed', after)
            if operation == 'update':
                patch = self.projects._unified_diff(path, (before or b'').decode('utf-8'), after.decode('utf-8'), before is None)
                bundle.writestr('change.patch', patch)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.root / f'{change_id}.zip'
        with target.open('xb') as output:
            output.write(buffer.getvalue())
            output.flush()
            os.fsync(output.fileno())
        self.projects._fsync_directory(self.root)
        return {**manifest, 'status': 'staged', 'download_url': f'/api/project-changes/{change_id}',
            'message': 'Change staged for owner integration. The live project has not been modified.'}

    def apply_file(self, path, content, expected_sha256, change_token):
        self.projects._assert_editable(path)
        preview = self.projects.preview_file(path, content)
        if preview['expected_sha256'] != expected_sha256 or preview['change_token'] != change_token:
            raise ValueError('Project changed or the proposed content differs from its preview; preview again')
        if expected_sha256 == 'absent':
            before, mode = None, 0o644
        else:
            before, mode = self._snapshot(path)
            if hashlib.sha256(before).hexdigest() != expected_sha256:
                raise ValueError('Project changed after preview; preview again')
        return self._save('update', path, expected_sha256, before, content.encode('utf-8'), mode=mode)

    def delete_file(self, path, expected_sha256):
        self.projects._assert_editable(path)
        before, mode = self._snapshot(path)
        if hashlib.sha256(before).hexdigest() != expected_sha256:
            raise ValueError('Project changed after inspection; inspect again')
        return self._save('delete', path, expected_sha256, before, mode=mode)

    def move_file(self, source_path, target_path, expected_sha256):
        self.projects._assert_editable(source_path)
        self.projects._assert_editable(target_path)
        source = self.projects._existing_file(source_path)
        target = self.projects._target(target_path)
        root = self.projects.root.resolve()
        if source.relative_to(root).parts[0] != target.relative_to(root).parts[0] or Path(source_path).parts[0] != Path(target_path).parts[0]:
            raise ValueError('Project moves must stay within the same project')
        if os.path.lexists(target):
            raise ValueError('Move target already exists')
        before, mode = self._snapshot(source_path)
        if hashlib.sha256(before).hexdigest() != expected_sha256:
            raise ValueError('Project changed after inspection; inspect again')
        return self._save('move', source_path, expected_sha256, before, before, target_path, mode)

    def download(self, change_id: UUID):
        path = self.root / f'{change_id}.zip'
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError('Project change not found')
        return path
