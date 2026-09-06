from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from .local import LocalStorageService


class ProjectFolderService(LocalStorageService):
    """Bounded project browser and safe single-file mutation service."""

    _HIDDEN_NAMES: ClassVar[set[str]] = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "node_modules"}
    _PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "README.md", "docker-compose.yml", "compose.yml")
    _PROTECTED_COMPONENTS: ClassVar[set[str]] = {".git", "secrets", ".secrets", "credentials", "private-keys", "deployment-keys"}
    _PROTECTED_FILENAMES: ClassVar[set[str]] = {"id_rsa", "id_ed25519", "authorized_keys", "known_hosts"}
    _PROTECTED_SUFFIXES: ClassVar[set[str]] = {".pem", ".p12", ".pfx", ".key"}
    _MAX_EDIT_BYTES = 4 * 1024 * 1024
    _MAX_CHECKPOINT_BYTES = 100 * 1024 * 1024
    _MAX_CHECKPOINT_FILES = 1000

    def __init__(self, root: Path, display_root: str, checkpoint_root: Path | None = None) -> None:
        super().__init__(root, display_root)
        self.checkpoint_root = checkpoint_root

    def list_directory(self, relative_path: str = "") -> dict:
        listing = super().list_directory(relative_path)
        listing["entries"] = [item for item in listing["entries"] if item["name"] not in self._HIDDEN_NAMES]
        if not relative_path.strip("/"):
            root = self.root.resolve(strict=True)
            listing["entries"] = [
                item for item in listing["entries"]
                if item["kind"] == "directory"
                and not item["name"].startswith(".")
                and any((root / item["name"] / marker).exists() for marker in self._PROJECT_MARKERS)
            ]
        listing["name"] = "Project folders"
        return listing

    def acquire_file(self, relative_path: str, *, max_bytes: int = 25 * 1024 * 1024) -> dict:
        result = super().acquire_file(relative_path, max_bytes=max_bytes)
        path = self._existing_file(relative_path)
        stat_result = path.stat()
        result["resource"]["sha256"] = self._sha256(path)
        result["resource"]["modified_at"] = datetime.fromtimestamp(stat_result.st_mtime, UTC).isoformat()
        return result

    def preview_file(self, relative_path: str, content: str) -> dict:
        target = self._target(relative_path)
        self._assert_editable(relative_path)
        proposed = content.encode("utf-8")
        if len(proposed) > self._MAX_EDIT_BYTES:
            raise ValueError("Project edits are limited to 4 MB per file")
        if target.exists():
            current = self._existing_file(relative_path)
            before_bytes = current.read_bytes()
            try:
                before = before_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Safe project editing currently supports UTF-8 text files only") from exc
            before_sha = hashlib.sha256(before_bytes).hexdigest()
            mode = stat.S_IMODE(current.stat().st_mode)
        else:
            before = ""
            before_sha = "absent"
            mode = 0o664
        after_sha = hashlib.sha256(proposed).hexdigest()
        token = self._change_token(relative_path, before_sha, after_sha)
        return {
            "path": relative_path,
            "change": "create" if before_sha == "absent" else "update",
            "expected_sha256": before_sha,
            "proposed_sha256": after_sha,
            "change_token": token,
            "mode": oct(mode),
            "diff": self._unified_diff(relative_path, before, content, before_sha == "absent"),
        }

    def apply_file(self, relative_path: str, content: str, expected_sha256: str, change_token: str) -> dict:
        target = self._target(relative_path)
        self._assert_editable(relative_path)
        proposed = content.encode("utf-8")
        if len(proposed) > self._MAX_EDIT_BYTES:
            raise ValueError("Project edits are limited to 4 MB per file")
        after_sha = hashlib.sha256(proposed).hexdigest()
        if change_token != self._change_token(relative_path, expected_sha256, after_sha):
            raise ValueError("The project edit does not match its preview")

        exists = target.exists()
        if expected_sha256 == "absent":
            if exists:
                raise ValueError("File appeared after Atlas previewed the change; refusing to overwrite it")
            before = ""
            mode = 0o664
        else:
            current = self._existing_file(relative_path)
            current_sha = self._sha256(current)
            if current_sha != expected_sha256:
                raise ValueError("File changed after Atlas read it; refusing to overwrite newer work")
            try:
                before = current.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Safe project editing currently supports UTF-8 text files only") from exc
            mode = stat.S_IMODE(current.stat().st_mode)

        checkpoint = self._checkpoint(target, relative_path)
        if expected_sha256 == "absent":
            if target.exists():
                raise ValueError("File changed while Atlas was checkpointing; refusing the write")
        elif self._sha256(self._existing_file(relative_path)) != expected_sha256:
            raise ValueError("File changed while Atlas was checkpointing; refusing the write")

        target.parent.mkdir(parents=False, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.atlas-", dir=target.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(proposed)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, mode)
            os.replace(temp_path, target)
            self._fsync_directory(target.parent)
        finally:
            temp_path.unlink(missing_ok=True)

        return {
            "path": relative_path,
            "status": "created" if expected_sha256 == "absent" else "updated",
            "before_sha256": expected_sha256,
            "after_sha256": after_sha,
            "checkpoint": checkpoint,
            "diff": self._unified_diff(relative_path, before, content, expected_sha256 == "absent"),
        }

    def move_file(self, source_path: str, target_path: str, expected_sha256: str) -> dict:
        self._assert_editable(source_path)
        self._assert_editable(target_path)
        source = self._existing_file(source_path)
        target = self._target(target_path)
        if source_path.split("/", 1)[0] != target_path.split("/", 1)[0]:
            raise ValueError("Project moves must stay within the same project")
        if target.exists():
            raise ValueError("Move target already exists")
        if self._sha256(source) != expected_sha256:
            raise ValueError("Source changed after Atlas read it; refusing to move newer work")
        checkpoint = self._checkpoint(source, source_path)
        if self._sha256(self._existing_file(source_path)) != expected_sha256:
            raise ValueError("Source changed while Atlas was checkpointing; refusing the move")
        target.parent.resolve(strict=True)
        os.replace(source, target)
        self._fsync_directory(source.parent)
        if target.parent != source.parent:
            self._fsync_directory(target.parent)
        return {"status": "moved", "from": source_path, "to": target_path, "sha256": expected_sha256, "checkpoint": checkpoint}

    def delete_file(self, relative_path: str, expected_sha256: str) -> dict:
        self._assert_editable(relative_path)
        target = self._existing_file(relative_path)
        if self._sha256(target) != expected_sha256:
            raise ValueError("File changed after Atlas read it; refusing to delete newer work")
        checkpoint = self._checkpoint(target, relative_path)
        if self._sha256(self._existing_file(relative_path)) != expected_sha256:
            raise ValueError("File changed while Atlas was checkpointing; refusing the delete")
        target.unlink()
        self._fsync_directory(target.parent)
        return {"status": "deleted", "path": relative_path, "sha256": expected_sha256, "checkpoint": checkpoint}

    def git_status(self, project: str) -> dict:
        project_path = self._project_root(project)
        head = self._git(project_path, "rev-parse", "HEAD", check=False).strip() or None
        status = self._git(project_path, "status", "--short", "--branch")
        return {"project": project, "head": head, "clean": not any(line and not line.startswith("##") for line in status.splitlines()), "status": status}

    def git_diff(self, project: str) -> dict:
        project_path = self._project_root(project)
        diff = self._git(project_path, "diff", "--binary", "HEAD", check=False)
        if len(diff.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("Git diff exceeds the 2 MB inspection limit")
        return {"project": project, "diff": diff}

    def _project_root(self, project: str) -> Path:
        if not project or "/" in project or project in {".", ".."}:
            raise ValueError("A top-level project name is required")
        root = self.root.resolve(strict=True)
        path = (root / project).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_dir():
            raise ValueError("Project is outside the approved project root")
        return path

    def _existing_file(self, relative_path: str) -> Path:
        root = self.root.resolve(strict=True)
        requested = (root / relative_path).resolve(strict=True)
        if not requested.is_relative_to(root):
            raise ValueError("Path is outside the approved project root")
        if not requested.is_file():
            raise FileNotFoundError(relative_path)
        return requested

    def _target(self, relative_path: str) -> Path:
        if not relative_path or relative_path.startswith("/"):
            raise ValueError("A path relative to the approved project root is required")
        root = self.root.resolve(strict=True)
        raw = Path(relative_path)
        if any(part in {"", ".", ".."} for part in raw.parts):
            raise ValueError("Project path contains an unsafe component")
        parent = (root / raw.parent).resolve(strict=True)
        if not parent.is_relative_to(root):
            raise ValueError("Path is outside the approved project root")
        target = parent / raw.name
        if target.is_symlink():
            resolved = target.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError("Symlink target escapes the approved project root")
            self._assert_editable(resolved.relative_to(root).as_posix())
            return resolved
        self._assert_editable(target.relative_to(root).as_posix())
        return target

    def _assert_editable(self, relative_path: str) -> None:
        parts = [part.casefold() for part in Path(relative_path).parts]
        name = parts[-1] if parts else ""
        if any(part in self._PROTECTED_COMPONENTS for part in parts):
            raise ValueError("Protected project paths cannot be modified by the normal project-write capability")
        if name == ".env" or name.startswith(".env.") or name in self._PROTECTED_FILENAMES:
            raise ValueError("Environment, credential, and key files are protected from normal project writes")
        if Path(name).suffix.casefold() in self._PROTECTED_SUFFIXES:
            raise ValueError("Private key material is protected from normal project writes")
        if "credential" in name or "token" in name:
            raise ValueError("Credential and token files are protected from normal project writes")

    def _checkpoint(self, target: Path, relative_path: str) -> dict:
        root = self.root.resolve(strict=True)
        project_name = Path(relative_path).parts[0]
        project = (root / project_name).resolve(strict=True)
        git_root_text = self._git(project, "rev-parse", "--show-toplevel", check=False).strip()
        if git_root_text:
            git_root = Path(git_root_text).resolve(strict=True)
            if not git_root.is_relative_to(root):
                raise ValueError("Git repository root escapes the approved project root")
            head = self._git(git_root, "rev-parse", "HEAD", check=False).strip() or None
            status = self._git(git_root, "status", "--porcelain", "--untracked-files=all", check=False)
            if not status.strip():
                return {"kind": "clean_git_baseline", "head": head, "project": git_root.name}
            return self._write_dirty_checkpoint(git_root, head, status, relative_path)
        return self._write_file_checkpoint(target, relative_path)

    def _write_dirty_checkpoint(self, git_root: Path, head: str | None, status: str, relative_path: str) -> dict:
        checkpoint = self._new_checkpoint_dir(git_root.name)
        patch = self._git(git_root, "diff", "--binary", "HEAD", check=False)
        patch_bytes = patch.encode("utf-8")
        if len(patch_bytes) > self._MAX_CHECKPOINT_BYTES:
            raise ValueError("Dirty Git checkpoint exceeds the 100 MB safety limit")
        (checkpoint / "working.patch").write_bytes(patch_bytes)
        untracked_raw = self._git_bytes(git_root, "ls-files", "--others", "--exclude-standard", "-z")
        untracked = [item.decode("utf-8", errors="surrogateescape") for item in untracked_raw.split(b"\0") if item]
        if len(untracked) > self._MAX_CHECKPOINT_FILES:
            raise ValueError("Dirty Git checkpoint contains too many untracked files")
        total = len(patch_bytes)
        for relative in untracked:
            source = (git_root / relative).resolve(strict=True)
            if not source.is_relative_to(git_root) or not source.is_file() or source.is_symlink():
                continue
            total += source.stat().st_size
            if total > self._MAX_CHECKPOINT_BYTES:
                raise ValueError("Dirty Git checkpoint exceeds the 100 MB safety limit")
            destination = checkpoint / "untracked" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        metadata = {"kind": "dirty_git_checkpoint", "project": git_root.name, "head": head, "status": status, "trigger_path": relative_path, "created_at": datetime.now(UTC).isoformat()}
        (checkpoint / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return {"kind": "dirty_git_checkpoint", "id": checkpoint.name, "head": head, "project": git_root.name}

    def _write_file_checkpoint(self, target: Path, relative_path: str) -> dict:
        checkpoint = self._new_checkpoint_dir(Path(relative_path).parts[0])
        metadata = {"kind": "filesystem_checkpoint", "trigger_path": relative_path, "existed": target.exists(), "created_at": datetime.now(UTC).isoformat()}
        if target.exists() and target.is_file():
            backup = checkpoint / "file"
            shutil.copy2(target, backup)
            metadata["sha256"] = self._sha256(target)
        (checkpoint / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return {"kind": "filesystem_checkpoint", "id": checkpoint.name, "project": Path(relative_path).parts[0]}

    def _new_checkpoint_dir(self, project: str) -> Path:
        if self.checkpoint_root is None:
            raise ValueError("Project checkpoint storage is not configured")
        self.checkpoint_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        path = self.checkpoint_root / f"{project}-{stamp}-{uuid4().hex[:8]}"
        path.mkdir(mode=0o700)
        return path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _change_token(relative_path: str, before_sha: str, after_sha: str) -> str:
        payload = json.dumps({"path": relative_path, "before": before_sha, "after": after_sha}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _unified_diff(relative_path: str, before: str, after: str, created: bool = False) -> str:
        from_name = "/dev/null" if created else f"a/{relative_path}"
        to_name = f"b/{relative_path}"
        return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile=from_name, tofile=to_name))

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _git(cwd: Path, *arguments: str, check: bool = True) -> str:
        completed = subprocess.run(["git", "-c", f"safe.directory={cwd}", "-C", str(cwd), *arguments], capture_output=True, text=True, timeout=20, check=False)
        if check and completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or "Git command failed")
        return completed.stdout

    @staticmethod
    def _git_bytes(cwd: Path, *arguments: str) -> bytes:
        completed = subprocess.run(["git", "-c", f"safe.directory={cwd}", "-C", str(cwd), *arguments], capture_output=True, timeout=20, check=False)
        if completed.returncode != 0:
            raise ValueError(completed.stderr.decode(errors="replace").strip() or "Git command failed")
        return completed.stdout
