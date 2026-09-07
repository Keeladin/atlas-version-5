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
