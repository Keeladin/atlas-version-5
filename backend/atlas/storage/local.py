import base64
import mimetypes
from datetime import UTC, datetime
from pathlib import Path


class LocalStorageService:
    def __init__(self, root: Path, display_root: str) -> None:
        self.root = root
        self.display_root = display_root

    def list_directory(self, relative_path: str = "") -> dict:
        root = self.root.resolve(strict=True)
        requested = (root / relative_path).resolve(strict=True)
        if not requested.is_relative_to(root):
            raise ValueError("Path is outside the approved workspace root")
        if not requested.is_dir():
            raise NotADirectoryError(relative_path)

        entries: list[dict] = []
        for child in requested.iterdir():
            try:
                resolved = child.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_relative_to(root):
                continue
            stat = child.stat()
            child_relative = child.relative_to(root).as_posix()
            entries.append({
                "name": child.name,
                "path": child_relative,
                "kind": "directory" if child.is_dir() else "file",
                "size_bytes": None if child.is_dir() else stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            })

        entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold()))
        return {
            "name": "Workspace",
            "display_root": self.display_root,
            "path": requested.relative_to(root).as_posix() if requested != root else "",
            "entries": entries,
        }

    def acquire_file(self, relative_path: str, *, max_bytes: int = 25 * 1024 * 1024) -> dict:
        root = self.root.resolve(strict=True)
        requested = (root / relative_path).resolve(strict=True)
        if not requested.is_relative_to(root):
            raise ValueError("Path is outside the approved workspace root")
        if not requested.is_file():
            raise FileNotFoundError(relative_path)
        stat = requested.stat()
        if stat.st_size > max_bytes:
            raise ValueError(f"File exceeds the {max_bytes // (1024 * 1024)} MB model-acquisition limit")
        media_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
        return {
            "resource": {
                "name": requested.name,
                "path": requested.relative_to(root).as_posix(),
                "media_type": media_type,
                "size_bytes": stat.st_size,
                "source": "local_workspace",
                "data_base64": base64.b64encode(requested.read_bytes()).decode("ascii"),
            }
        }

    def store_file(self, relative_directory: str, filename: str, data: bytes) -> dict:
        root = self.root.resolve(strict=True)
        directory = (root / relative_directory).resolve(strict=True)
        if not directory.is_relative_to(root):
            raise ValueError("Path is outside the approved workspace root")
        if not directory.is_dir():
            raise NotADirectoryError(relative_directory)

        safe_name = Path(filename).name.strip()
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError("A valid filename is required")

        target = directory / safe_name
        stem, suffix = target.stem, target.suffix
        counter = 2
        while target.exists():
            target = directory / f"{stem} ({counter}){suffix}"
            counter += 1

        target.write_bytes(data)
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root):
            target.unlink(missing_ok=True)
            raise ValueError("Upload target escaped the approved workspace root")
        stat = target.stat()
        return {
            "name": target.name,
            "path": target.relative_to(root).as_posix(),
            "kind": "file",
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        }
