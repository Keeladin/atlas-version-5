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

    def acquire_file(
        self,
        relative_path: str,
        *,
        max_bytes: int = 25 * 1024 * 1024,
        start_line: int | None = None,
        max_lines: int | None = None,
    ) -> dict:
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
        raw = requested.read_bytes()
        range_meta = None
        if start_line is not None or max_lines is not None:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Line-range acquisition is available only for UTF-8 text files") from exc
            lines = text.splitlines(keepends=True)
            first = max(1, int(start_line or 1))
            count = max(1, min(int(max_lines or 400), 2000))
            selected = lines[first - 1 : first - 1 + count]
            raw = "".join(selected).encode("utf-8")
            end_line = first + len(selected) - 1 if selected else first - 1
            range_meta = {
                "start_line": first,
                "end_line": end_line,
                "total_lines": len(lines),
                "complete": end_line >= len(lines),
            }
        resource = {
            "name": requested.name,
            "path": requested.relative_to(root).as_posix(),
            "media_type": media_type,
            "size_bytes": stat.st_size,
            "projected_size_bytes": len(raw),
            "source": "local_workspace",
            "data_base64": base64.b64encode(raw).decode("ascii"),
        }
        if range_meta is not None:
            resource["range"] = range_meta
        return {"resource": resource}

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
