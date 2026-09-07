from __future__ import annotations

import ctypes
import difflib
import errno
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from .local import LocalStorageService


def serialized_mutation(method):
    @wraps(method)
    def call(self, relative_path, *args, **kwargs):
        self._assert_editable(relative_path)
        if self.checkpoint_root is None:
            raise ValueError("Project checkpoint storage is not configured")
        project = (self.root / relative_path).resolve(strict=False).relative_to(self.root.resolve()).parts[0]
        locks = self.checkpoint_root / "locks"
        locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(str(self.root.resolve() / project).encode()).hexdigest()
        with (locks / key).open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return method(self, relative_path, *args, **kwargs)
    return call


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
        if relative_path.strip("/"):
            self._assert_readable(relative_path)
        listing = super().list_directory(relative_path)
        listing["entries"] = [
            item
            for item in listing["entries"]
            if item["name"] not in self._HIDDEN_NAMES
            and self._allowed_for_export(item["path"])
        ]
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

    def acquire_file(
        self,
        relative_path: str,
        *,
        max_bytes: int = 25 * 1024 * 1024,
        start_line: int | None = None,
        max_lines: int | None = None,
    ) -> dict:
        self._assert_readable(relative_path)
        path = self._existing_file(relative_path)
        root = self.root.resolve(strict=True)
        self._assert_readable(path.relative_to(root).as_posix())
        result = super().acquire_file(
            relative_path, max_bytes=max_bytes, start_line=start_line, max_lines=max_lines
        )
        result["resource"]["source"] = "project_folder"
        return result

    @contextmanager
    def _pinned_target(self, relative_path: str, *, follow_final: bool = True):
        """Resolve permitted aliases, then traverse without following new symlinks.

        All subsequent filesystem sinks use the pinned directory descriptor,
        so replacing a pathname with a symlink cannot redirect the operation.
        """
        self._assert_readable(relative_path)
        root = self.root.resolve(strict=True)
        raw = root / relative_path
        target = self._target(relative_path) if follow_final else raw.parent.resolve(strict=True) / raw.name
        canonical = target.relative_to(root)
        self._assert_readable(canonical.as_posix())
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in canonical.parent.parts:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            actual_parent = Path(os.readlink(f"/proc/self/fd/{fd}"))
            self._assert_readable((actual_parent / target.name).relative_to(root).as_posix())
            yield Path(f"/proc/self/fd/{fd}") / target.name, target
            if Path(os.readlink(f"/proc/self/fd/{fd}")) != target.parent:
                raise ValueError("Project directory changed during the operation; verify its outcome before retrying")
        finally:
            os.close(fd)

    def _read_checked(self, path: Path, max_bytes: int):
        relative = path.relative_to(self.root.resolve(strict=True)).as_posix()
        with self._pinned_target(relative) as (pinned, canonical):
            fd = os.open(pinned, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Only regular project files may be read")
                if info.st_size > max_bytes:
                    raise ValueError("Project file exceeds the acquisition/checkpoint size limit")
                raw = handle.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise ValueError("Project file exceeds the acquisition/checkpoint size limit")
        return canonical, raw, info

    def _acquisition_snapshot(self, relative_path: str, max_bytes: int):
        self._assert_readable(relative_path)
        return self._read_checked(self.root.resolve(strict=True) / relative_path, max_bytes)

    def preview_file(self, relative_path: str, content: str) -> dict:
        target = self._target(relative_path)
        self._assert_editable(relative_path)
        proposed = content.encode("utf-8")
        if len(proposed) > self._MAX_EDIT_BYTES:
            raise ValueError("Project edits are limited to 4 MB per file")
        if target.exists():
            current = self._existing_file(relative_path)
            _, before_bytes, info = self._read_checked(current, self._MAX_EDIT_BYTES)
            try:
                before = before_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Safe project editing currently supports UTF-8 text files only") from exc
            before_sha = hashlib.sha256(before_bytes).hexdigest()
            mode = stat.S_IMODE(info.st_mode)
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

    @serialized_mutation
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
            _, before_bytes, info = self._read_checked(current, self._MAX_EDIT_BYTES)
            current_sha = hashlib.sha256(before_bytes).hexdigest()
            if current_sha != expected_sha256:
                raise ValueError("File changed after Atlas read it; refusing to overwrite newer work")
            try:
                before = before_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Safe project editing currently supports UTF-8 text files only") from exc
            mode = stat.S_IMODE(info.st_mode)

        checkpoint = self._checkpoint(target, relative_path)
        if expected_sha256 == "absent":
            if target.exists():
                raise ValueError("File changed while Atlas was checkpointing; refusing the write")
        elif self._sha256(self._existing_file(relative_path)) != expected_sha256:
            raise ValueError("File changed while Atlas was checkpointing; refusing the write")

        with self._pinned_target(relative_path) as (pinned, _):
            fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.atlas-", dir=pinned.parent)
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(proposed)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp_path, mode)
                if expected_sha256 == "absent":
                    self._rename_noreplace(temp_path, pinned)
                else:
                    os.replace(temp_path, pinned)
                self._fsync_directory(pinned.parent)
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

    @serialized_mutation
    def move_file(self, source_path: str, target_path: str, expected_sha256: str) -> dict:
        self._assert_editable(source_path)
        self._assert_editable(target_path)
        source = self._existing_file(source_path)
        target = self._target(target_path)
        root = self.root.resolve(strict=True)
        if (source.relative_to(root).parts[0] != target.relative_to(root).parts[0]
                or source_path.split("/", 1)[0] != target_path.split("/", 1)[0]):
            raise ValueError("Project moves must stay within the same project")
        if os.path.lexists(target):
            raise ValueError("Move target already exists")
        if self._sha256(source) != expected_sha256:
            raise ValueError("Source changed after Atlas read it; refusing to move newer work")
        checkpoint = self._checkpoint(source, source_path)
        if self._sha256(self._existing_file(source_path)) != expected_sha256:
            raise ValueError("Source changed while Atlas was checkpointing; refusing the move")
        with self._pinned_target(source_path) as (pinned_source, _), self._pinned_target(target_path) as (pinned_target, _):
            self._rename_noreplace(pinned_source, pinned_target)
            self._fsync_directory(pinned_source.parent)
            self._fsync_directory(pinned_target.parent)
        return {"status": "moved", "from": source_path, "to": target_path, "sha256": expected_sha256, "checkpoint": checkpoint}

    @serialized_mutation
    def delete_file(self, relative_path: str, expected_sha256: str) -> dict:
        self._assert_editable(relative_path)
        target = self._existing_file(relative_path)
        if self._sha256(target) != expected_sha256:
            raise ValueError("File changed after Atlas read it; refusing to delete newer work")
        checkpoint = self._checkpoint(target, relative_path)
        if self._sha256(self._existing_file(relative_path)) != expected_sha256:
            raise ValueError("File changed while Atlas was checkpointing; refusing the delete")
        with self._pinned_target(relative_path) as (pinned, _):
            pinned.unlink()
            self._fsync_directory(pinned.parent)
        return {"status": "deleted", "path": relative_path, "sha256": expected_sha256, "checkpoint": checkpoint}

    def git_status(self, project: str) -> dict:
        project_path = self._project_root(project)
        head = self._git(project_path, "rev-parse", "HEAD", check=False).strip() or None
        status = self._git(project_path, "status", "--short", "--branch")
        return {"project": project, "head": head, "clean": not any(line and not line.startswith("##") for line in status.splitlines()), "status": status}

    def git_diff(self, project: str) -> dict:
        project_path = self._project_root(project)
        diff, excluded = self._safe_git_diff(project_path)
        if len(diff.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("Git diff exceeds the 2 MB inspection limit")
        return {"project": project, "diff": diff, "protected_paths_excluded": excluded}

    def _project_root(self, project: str) -> Path:
        if not project or "/" in project or project in {".", ".."}:
            raise ValueError("A top-level project name is required")
        self._assert_readable(project)
        root = self.root.resolve(strict=True)
        path = (root / project).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_dir():
            raise ValueError("Project is outside the approved project root")
        return path

    def _existing_file(self, relative_path: str) -> Path:
        self._assert_readable(relative_path)
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

    def _protected_reason(self, relative_path: str) -> str | None:
        parts = [part.casefold() for part in Path(relative_path).parts]
        name = parts[-1] if parts else ""
        if any(part in self._PROTECTED_COMPONENTS for part in parts):
            return "protected project path"
        if name == ".env" or name.startswith(".env.") or name in self._PROTECTED_FILENAMES:
            return "environment, credential, or key file"
        if Path(name).suffix.casefold() in self._PROTECTED_SUFFIXES:
            return "private key material"
        if "credential" in name or "token" in name:
            return "credential or token file"
        return None

    def _assert_allowed(self, relative_path: str, *, writing: bool = False) -> None:
        root = self.root.resolve(strict=True)
        candidate = root / relative_path
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ValueError("Path is outside the approved project root")
        if (self._protected_reason(relative_path) is not None
                or self._protected_reason(resolved.relative_to(root).as_posix()) is not None):
            verb = "modified" if writing else "read"
            raise ValueError(f"protected project material cannot be {verb} by the normal project capability")

    def _assert_readable(self, relative_path: str) -> None:
        self._assert_allowed(relative_path)

    def _assert_editable(self, relative_path: str) -> None:
        self._assert_allowed(relative_path, writing=True)

    def _allowed_for_export(self, relative_path: str) -> bool:
        try:
            self._assert_readable(relative_path)
        except (ValueError, OSError):
            return False
        return True

    def _safe_git_diff(self, project: Path) -> tuple[str, int]:
        # Disable external helpers and inspect NUL-separated paths, including
        # both sides of renames, before allowing Git to render content.
        baseline = "HEAD" if self._git(project, "rev-parse", "--verify", "HEAD", check=False).strip() else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        raw = self._git_bytes(project, "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", "--find-renames", baseline)
        fields = raw.split(b"\0")
        allowed: set[str] = set()
        excluded = 0
        index = 0
        root = self.root.resolve(strict=True)
        while index < len(fields) and fields[index]:
            status = fields[index].decode('ascii')
            count = 2 if status.startswith(('R', 'C')) else 1
            paths = [part.decode('utf-8', errors='surrogateescape') for part in fields[index + 1:index + 1 + count]]
            index += 1 + count
            if all(self._allowed_for_export((project / path).relative_to(root).as_posix()) for path in paths):
                allowed.update(paths)
            else:
                excluded += len(paths)
        if not allowed:
            return "", excluded
        # Render only immutable permitted snapshots. Git must not reopen the
        # mutable worktree after our path policy check (parent aliases can race).
        with tempfile.TemporaryDirectory(prefix="atlas-project-diff-") as temporary:
            snapshot_root = Path(temporary)
            (snapshot_root / "a").mkdir()
            (snapshot_root / "b").mkdir()
            total = 0
            for path in sorted(allowed):
                current = project / path
                relative = current.relative_to(root).as_posix()
                self._assert_readable(relative)
                object_name = f"{baseline}:{path}"
                exists = self._git(project, "cat-file", "-t", object_name, check=False).strip()
                if exists == "blob":
                    size = int(self._git(project, "cat-file", "-s", object_name).strip())
                    if total + size > self._MAX_CHECKPOINT_BYTES:
                        raise ValueError("Project diff snapshot exceeds the size limit")
                    before = self._git_bytes(project, "cat-file", "blob", object_name)
                    destination = snapshot_root / "a" / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    mode = self._git(project, "ls-tree", "--format=%(objectmode)", baseline, "--", f":(literal){path}").strip()
                    if mode == "120000":
                        destination.symlink_to(os.fsdecode(before))
                    else:
                        destination.write_bytes(before)
                        destination.chmod(0o755 if mode == "100755" else 0o644)
                    total += len(before)
                with self._pinned_target(relative, follow_final=False) as (pinned, _):
                    if pinned.is_symlink():
                        destination = snapshot_root / "b" / path
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.symlink_to(os.readlink(pinned))
                        continue
                try:
                    _, after, info = self._read_checked(current, self._MAX_CHECKPOINT_BYTES - total)
                except FileNotFoundError:
                    continue
                destination = snapshot_root / "b" / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(after)
                destination.chmod(stat.S_IMODE(info.st_mode))
                total += len(after)
            completed = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                "diff", "--no-index", "--no-ext-diff", "--no-textconv", "--binary", "--src-prefix=", "--dst-prefix=", "a", "b"],
                cwd=snapshot_root, capture_output=True, timeout=20, check=False)
            if completed.returncode not in {0, 1}:
                raise ValueError("Could not render the permitted project snapshots")
            return completed.stdout.decode('utf-8'), excluded

    @staticmethod
    def _rename_noreplace(source: Path, target: Path) -> None:
        # Linux is the deployed platform. Fail closed if the atomic primitive
        # is unavailable; check-then-replace is not a safe fallback.
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, 'renameat2', None)
        if rename is None:
            raise OSError("Atomic no-replace rename is unavailable")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise ValueError("Move target already exists; refusing to overwrite newer work")
            raise OSError(code, os.strerror(code))

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
        patch, excluded = self._safe_git_diff(git_root)
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
            raw_source = git_root / relative
            if raw_source.is_symlink() or not self._allowed_for_export(raw_source.relative_to(self.root.resolve()).as_posix()):
                excluded += 1
                continue
            source = raw_source.resolve(strict=True)
            if not source.is_relative_to(git_root) or not source.is_file():
                excluded += 1
                continue
            _, snapshot, info = self._read_checked(source, self._MAX_CHECKPOINT_BYTES - total)
            total += len(snapshot)
            if total > self._MAX_CHECKPOINT_BYTES:
                raise ValueError("Dirty Git checkpoint exceeds the 100 MB safety limit")
            destination = checkpoint / "untracked" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(snapshot)
            destination.chmod(stat.S_IMODE(info.st_mode))
        metadata = {"kind": "dirty_git_checkpoint", "project": git_root.name, "head": head, "protected_paths_excluded": excluded, "scope": "permitted project files only", "trigger_path": relative_path, "created_at": datetime.now(UTC).isoformat()}
        (checkpoint / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return {"kind": "dirty_git_checkpoint", "id": checkpoint.name, "head": head, "project": git_root.name, "protected_paths_excluded": excluded}

    def _write_file_checkpoint(self, target: Path, relative_path: str) -> dict:
        checkpoint = self._new_checkpoint_dir(Path(relative_path).parts[0])
        metadata = {"kind": "filesystem_checkpoint", "trigger_path": relative_path, "existed": target.exists(), "created_at": datetime.now(UTC).isoformat()}
        if target.exists() and target.is_file():
            backup = checkpoint / "file"
            _, snapshot, info = self._read_checked(target, self._MAX_CHECKPOINT_BYTES)
            backup.write_bytes(snapshot)
            backup.chmod(stat.S_IMODE(info.st_mode))
            metadata["sha256"] = hashlib.sha256(snapshot).hexdigest()
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

    def _sha256(self, path: Path) -> str:
        _, snapshot, _ = self._read_checked(path, self._MAX_CHECKPOINT_BYTES)
        return hashlib.sha256(snapshot).hexdigest()

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
        completed = subprocess.run(["git", "-c", f"safe.directory={cwd}", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(cwd), *arguments], capture_output=True, text=True, timeout=20, check=False)
        if check and completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or "Git command failed")
        return completed.stdout

    @staticmethod
    def _git_bytes(cwd: Path, *arguments: str) -> bytes:
        completed = subprocess.run(["git", "-c", f"safe.directory={cwd}", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(cwd), *arguments], capture_output=True, timeout=20, check=False)
        if completed.returncode != 0:
            raise ValueError(completed.stderr.decode(errors="replace").strip() or "Git command failed")
        return completed.stdout
