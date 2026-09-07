from pathlib import Path

import pytest
from atlas.storage import LocalStorageService, ProjectFolderService


def test_local_storage_lists_only_approved_root(tmp_path: Path) -> None:
    (tmp_path / "Projects").mkdir()
    (tmp_path / "note.txt").write_text("atlas")
    service = LocalStorageService(tmp_path, "/home/jaco/Workspace")

    listing = service.list_directory()

    assert listing["display_root"] == "/home/jaco/Workspace"
    assert [item["name"] for item in listing["entries"]] == ["Projects", "note.txt"]
    assert listing["entries"][0]["kind"] == "directory"
    assert listing["entries"][1]["size_bytes"] == 5


def test_local_storage_rejects_escape(tmp_path: Path) -> None:
    service = LocalStorageService(tmp_path, "/home/jaco/Workspace")

    with pytest.raises(ValueError):
        service.list_directory("..")


def test_local_storage_acquires_image_as_model_resource(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpeg-bytes")
    service = LocalStorageService(tmp_path, "/home/jaco/Workspace")

    acquired = service.acquire_file("photo.jpg")

    resource = acquired["resource"]
    assert resource["name"] == "photo.jpg"
    assert resource["media_type"] == "image/jpeg"
    assert resource["source"] == "local_workspace"
    assert resource["data_base64"] == "anBlZy1ieXRlcw=="


def test_local_storage_acquire_rejects_escape(tmp_path: Path) -> None:
    service = LocalStorageService(tmp_path, "/home/jaco/Workspace")

    with pytest.raises((FileNotFoundError, ValueError)):
        service.acquire_file("../outside.jpg")


def test_project_folders_show_real_projects_and_hide_runtime_noise(tmp_path: Path) -> None:
    project = tmp_path / "Morning"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "README.md").write_text("morning")
    state = tmp_path / "runtime-state"
    state.mkdir()
    service = ProjectFolderService(tmp_path, "/home/jaco/Projects")

    root = service.list_directory()
    assert [item["name"] for item in root["entries"]] == ["Morning"]

    inside = service.list_directory("Morning")
    assert [item["name"] for item in inside["entries"]] == ["README.md"]


def test_project_folders_reject_escape(tmp_path: Path) -> None:
    service = ProjectFolderService(tmp_path, "/home/jaco/Projects")
    with pytest.raises(ValueError):
        service.list_directory("..")


def _git_project(tmp_path: Path) -> tuple[Path, ProjectFolderService]:
    import subprocess

    project = tmp_path / "Demo"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "atlas-test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Atlas Test"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "baseline"], check=True)
    service = ProjectFolderService(tmp_path, "/home/jaco/Projects", tmp_path / ".checkpoints")
    return project, service


def test_project_acquire_returns_concurrency_hash(tmp_path: Path) -> None:
    project, service = _git_project(tmp_path)
    acquired = service.acquire_file("Demo/app.py")
    assert acquired["resource"]["sha256"] == service._sha256(project / "app.py")
    assert acquired["resource"]["modified_at"]


def test_project_preview_then_atomic_apply_uses_clean_git_baseline(tmp_path: Path) -> None:
    project, service = _git_project(tmp_path)
    original_mode = (project / "app.py").stat().st_mode & 0o777
    preview = service.preview_file("Demo/app.py", "value = 2\n")

    assert preview["change"] == "update"
    assert "-value = 1" in preview["diff"]
    assert "+value = 2" in preview["diff"]

    applied = service.apply_file(
        "Demo/app.py", "value = 2\n", preview["expected_sha256"], preview["change_token"]
    )
    assert (project / "app.py").read_text() == "value = 2\n"
    assert (project / "app.py").stat().st_mode & 0o777 == original_mode
    assert applied["checkpoint"]["kind"] == "clean_git_baseline"
    assert service.git_status("Demo")["clean"] is False


def test_project_apply_refuses_vscode_style_stale_overwrite(tmp_path: Path) -> None:
    project, service = _git_project(tmp_path)
    preview = service.preview_file("Demo/app.py", "value = 2\n")
    (project / "app.py").write_text("value = 99\n")

    with pytest.raises(ValueError, match="changed after Atlas read"):
        service.apply_file(
            "Demo/app.py", "value = 2\n", preview["expected_sha256"], preview["change_token"]
        )
    assert (project / "app.py").read_text() == "value = 99\n"


def test_project_dirty_state_is_checkpointed_before_second_change(tmp_path: Path) -> None:
    _, service = _git_project(tmp_path)
    first = service.preview_file("Demo/app.py", "value = 2\n")
    service.apply_file("Demo/app.py", "value = 2\n", first["expected_sha256"], first["change_token"])
    second = service.preview_file("Demo/app.py", "value = 3\n")
    applied = service.apply_file("Demo/app.py", "value = 3\n", second["expected_sha256"], second["change_token"])

    assert applied["checkpoint"]["kind"] == "dirty_git_checkpoint"
    checkpoint = service.checkpoint_root / applied["checkpoint"]["id"]
    assert (checkpoint / "metadata.json").is_file()
    assert (checkpoint / "working.patch").is_file()
    assert "value = 2" in (checkpoint / "working.patch").read_text()


def test_project_normal_write_blocks_environment_and_key_material(tmp_path: Path) -> None:
    project = tmp_path / "Demo"
    project.mkdir()
    (project / ".env").write_text("TOKEN=secret\n")
    service = ProjectFolderService(tmp_path, "/home/jaco/Projects", tmp_path / ".checkpoints")

    with pytest.raises(ValueError, match="protected"):
        service.preview_file("Demo/.env", "TOKEN=changed\n")
    with pytest.raises(ValueError, match="protected"):
        service.preview_file("Demo/deploy.pem", "key\n")


def test_project_write_cannot_bypass_protection_through_symlink(tmp_path: Path) -> None:
    project = tmp_path / "Demo"
    project.mkdir()
    (project / ".env").write_text("TOKEN=secret\n")
    (project / "safe.txt").symlink_to(project / ".env")
    service = ProjectFolderService(tmp_path, "/home/jaco/Projects", tmp_path / ".checkpoints")

    with pytest.raises(ValueError, match="protected"):
        service.preview_file("Demo/safe.txt", "TOKEN=changed\n")


def test_project_move_verifies_hash_and_checkpoints(tmp_path: Path) -> None:
    project, service = _git_project(tmp_path)
    expected = service.acquire_file("Demo/app.py")["resource"]["sha256"]
    result = service.move_file("Demo/app.py", "Demo/main.py", expected)
    assert result["status"] == "moved"
    assert not (project / "app.py").exists()
    assert (project / "main.py").read_text() == "value = 1\n"


def test_project_git_invocation_marks_repo_as_safe_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess

    seen: list[str] = []

    class Completed:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(command, **kwargs):
        seen.extend(command)
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ProjectFolderService._git(tmp_path, "status")

    assert result == "ok\n"
    assert "-c" in seen
    assert f"safe.directory={tmp_path}" in seen


def test_project_reads_hide_and_reject_protected_material(tmp_path: Path) -> None:
    project = tmp_path / "Demo"
    project.mkdir()
    (project / "README.md").write_text("demo\n")
    (project / ".env").write_text("TOKEN=secret\n")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("secret\n")
    (project / "secrets").mkdir()
    (project / "secrets" / "api-token.txt").write_text("secret\n")
    service = ProjectFolderService(tmp_path, "/home/jaco/Projects", tmp_path / ".checkpoints")

    listing = service.list_directory("Demo")
    assert [item["name"] for item in listing["entries"]] == ["README.md"]
    for path in ("Demo/.env", "Demo/.git/config", "Demo/secrets/api-token.txt"):
        with pytest.raises(ValueError, match="cannot be read"):
            service.acquire_file(path)
    with pytest.raises(ValueError, match="cannot be read"):
        service.list_directory("Demo/secrets")


def test_project_read_cannot_bypass_protection_through_symlink(tmp_path: Path) -> None:
    project = tmp_path / "Demo"
    project.mkdir()
    secrets = project / "secrets"
    secrets.mkdir()
    (secrets / "api.key").write_text("SUPER-SECRET-VALUE\n")
    (project / "notes.md").symlink_to(secrets / "api.key")
    service = ProjectFolderService(tmp_path, "/home/jaco/Projects", tmp_path / ".checkpoints")

    with pytest.raises(ValueError, match="protected project material cannot be read"):
        service.acquire_file("Demo/notes.md")


def test_local_storage_acquires_bounded_utf8_line_range(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\ntwo\nthree\nfour\nfive\n")
    service = LocalStorageService(tmp_path, "/home/jaco/Workspace")

    acquired = service.acquire_file("notes.txt", start_line=2, max_lines=2)
    resource = acquired["resource"]

    import base64

    assert base64.b64decode(resource["data_base64"]).decode() == "two\nthree\n"
    assert resource["size_bytes"] == path.stat().st_size
    assert resource["range"] == {"start_line": 2, "end_line": 3, "total_lines": 5, "complete": False}


def test_project_acquire_line_range_preserves_full_file_hash(tmp_path: Path) -> None:
    project, service = _git_project(tmp_path)
    (project / "app.py").write_text("one\ntwo\nthree\n")

    acquired = service.acquire_file("Demo/app.py", start_line=2, max_lines=1)

    assert acquired["resource"]["range"]["start_line"] == 2
    assert acquired["resource"]["range"]["end_line"] == 2
    assert acquired["resource"]["sha256"] == service._sha256(project / "app.py")


@pytest.mark.parametrize('operation', ['acquire', 'move', 'delete', 'list'])
def test_project_aliases_cannot_reach_protected_targets(tmp_path, operation):
    project, service = _git_project(tmp_path)
    protected = project / 'secrets'
    protected.mkdir()
    (protected / 'note.txt').write_text('SYNTHETIC_PROTECTED_VALUE')
    (project / 'alias').symlink_to(protected, target_is_directory=True)
    path = 'Demo/alias/note.txt'
    digest = __import__('hashlib').sha256((protected / 'note.txt').read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='protected'):
        if operation == 'acquire':
            service.acquire_file(path)
        elif operation == 'move':
            service.move_file(path, 'Demo/output.txt', digest)
        elif operation == 'delete':
            service.delete_file(path, digest)
        else:
            service.list_directory('Demo/alias')
    assert 'alias' not in [item['name'] for item in service.list_directory('Demo')['entries']]
    assert (protected / 'note.txt').read_text() == 'SYNTHETIC_PROTECTED_VALUE'


def test_move_does_not_overwrite_target_created_during_checkpoint(tmp_path, monkeypatch):
    project, service = _git_project(tmp_path)
    expected = service._sha256(project / 'app.py')
    original = service._checkpoint
    def checkpoint(*args):
        result = original(*args)
        (project / 'new.py').write_text('owner work')
        return result
    monkeypatch.setattr(service, '_checkpoint', checkpoint)
    with pytest.raises(ValueError, match='overwrite'):
        service.move_file('Demo/app.py', 'Demo/new.py', expected)
    assert (project / 'new.py').read_text() == 'owner work'
    assert (project / 'app.py').exists()


def test_git_diff_and_checkpoint_exclude_protected_and_alias_content(tmp_path):
    import subprocess
    project, service = _git_project(tmp_path)
    (project / '.env').write_text('PROTECTED_BEFORE')
    subprocess.run(['git', '-C', str(project), 'add', '.env'], check=True)
    subprocess.run(['git', '-C', str(project), 'commit', '-qm', 'fixture'], check=True)
    (project / '.env').write_text('PROTECTED_AFTER')
    (project / 'api-token.txt').write_text('PROTECTED_UNTRACKED')
    (project / 'alias.txt').symlink_to(project / '.env')
    (project / 'normal.txt').write_text('keep ordinary untracked file')
    (project / 'app.py').write_text('ordinary dirty edit\n')
    diff = service.git_diff('Demo')
    assert 'PROTECTED' not in diff['diff']
    assert 'ordinary dirty edit' in diff['diff']
    preview = service.preview_file('Demo/app.py', 'next change\n')
    result = service.apply_file('Demo/app.py', 'next change\n', preview['expected_sha256'], preview['change_token'])
    checkpoint = service.checkpoint_root / result['checkpoint']['id']
    assert (checkpoint / 'untracked' / 'normal.txt').read_text() == 'keep ordinary untracked file'
    for path in checkpoint.rglob('*'):
        if path.is_file():
            assert b'PROTECTED' not in path.read_bytes()
    assert result['checkpoint']['protected_paths_excluded'] > 0


def test_workspace_upload_never_follows_dangling_target_symlink(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    outside = tmp_path / 'outside'
    (root / 'note.txt').symlink_to(outside)
    uploaded = LocalStorageService(root, str(root)).store_file('', 'note.txt', b'ordinary data')
    assert not outside.exists()
    assert uploaded['path'] == 'note (2).txt'


def test_project_acquire_rechecks_alias_at_snapshot_boundary(tmp_path, monkeypatch):
    import base64
    project = tmp_path / 'demo'
    project.mkdir()
    (project / 'readme.txt').write_text('Ordinary file')
    (project / '.env').write_text('Synthetic protected fixture')
    alias = project / 'alias.txt'
    alias.symlink_to('readme.txt')
    service = ProjectFolderService(tmp_path, 'Projects', tmp_path / 'checkpoints')
    assert base64.b64decode(service.acquire_file('demo/alias.txt')['resource']['data_base64']) == b'Ordinary file'
    original = LocalStorageService.acquire_file
    def swap(self, *args, **kwargs):
        alias.unlink()
        alias.symlink_to('.env')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(LocalStorageService, 'acquire_file', swap)
    with pytest.raises(ValueError, match='protected'):
        service.acquire_file('demo/alias.txt')


def test_project_move_pins_target_parent_before_atomic_rename(tmp_path, monkeypatch):
    project = tmp_path / 'demo'
    project.mkdir()
    (project / 'public').mkdir()
    (project / 'secrets').mkdir()
    source = project / 'source.txt'
    source.write_text('Owner content')
    service = ProjectFolderService(tmp_path, 'Projects', tmp_path / 'checkpoints')
    digest = service._sha256(source)
    original = service._rename_noreplace
    def swap(source, target):
        (project / 'public').rename(project / 'saved')
        (project / 'public').symlink_to('secrets', target_is_directory=True)
        original(source, target)
    monkeypatch.setattr(service, '_rename_noreplace', swap)
    with pytest.raises(ValueError, match='directory changed'):
        service.move_file('demo/source.txt', 'demo/public/destination.txt', digest)
    assert not (project / 'secrets' / 'destination.txt').exists()
    assert (project / 'saved' / 'destination.txt').read_text() == 'Owner content'


def test_project_snapshot_refuses_final_symlink_swap(tmp_path, monkeypatch):
    import os
    project = tmp_path / 'demo'
    project.mkdir()
    source = project / 'note.txt'
    source.write_text('Ordinary')
    (project / '.env').write_text('Synthetic protected fixture')
    service = ProjectFolderService(tmp_path, 'Projects', tmp_path / 'checkpoints')
    original = os.open
    def swap(path, flags, *args, **kwargs):
        if str(path).startswith('/proc/self/fd/') and str(path).endswith('/note.txt'):
            source.unlink()
            source.symlink_to('.env')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', swap)
    with pytest.raises(OSError):
        service.acquire_file('demo/note.txt')
    assert not list((tmp_path / 'checkpoints').glob('**/file'))


def test_permitted_git_snapshot_patch_round_trips_modes_and_binary(tmp_path):
    import subprocess
    project, service = _git_project(tmp_path)
    (project / 'script.sh').write_text('#!/bin/sh\nexit 0\n')
    (project / 'script.sh').chmod(0o755)
    (project / 'data.bin').write_bytes(b'\x00\x01old')
    (project / 'link').symlink_to('app.py')
    subprocess.run(['git', '-C', str(project), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(project), 'commit', '-qm', 'permitted baseline'], check=True)
    (project / 'script.sh').write_text('#!/bin/sh\nexit 1\n')
    (project / 'data.bin').write_bytes(b'\x00\x01new')
    (project / 'link').unlink()
    (project / 'link').symlink_to('script.sh')
    patch = service.git_diff('Demo')['diff']
    result = subprocess.run(['git', '-C', str(project), 'apply', '--reverse', '--check'],
        input=patch, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
