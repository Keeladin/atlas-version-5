import json
import os
import subprocess
from pathlib import Path

_FOLDER_MIME = "application/vnd.google-apps.folder"


class GoogleWorkspaceService:
    def __init__(
        self,
        command: Path,
        credentials_file: Path | None,
        config_dir: Path,
        workspace_dir: Path,
    ) -> None:
        self.command = command
        self.credentials_file = credentials_file
        self.config_dir = config_dir
        self.workspace_dir = workspace_dir

    def auth_status(self) -> dict[str, object]:
        return self._run("auth", "status")

    def list_drive_folder(self, folder_id: str = "root") -> dict[str, object]:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "pageSize": 100,
            "fields": "files(id,name,mimeType,modifiedTime,size,parents,webViewLink),nextPageToken",
        }
        payload = self._run(
            "drive",
            "files",
            "list",
            "--params",
            json.dumps(params, separators=(",", ":")),
        )
        files = payload.get("files", []) if isinstance(payload, dict) else []
        entries = []
        for item in files:
            if not isinstance(item, dict):
                continue
            entries.append({
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "kind": "directory" if item.get("mimeType") == _FOLDER_MIME else "file",
                "mime_type": str(item.get("mimeType") or "application/octet-stream"),
                "size_bytes": int(item["size"]) if str(item.get("size") or "").isdigit() else None,
                "modified_at": item.get("modifiedTime"),
                "web_view_link": item.get("webViewLink"),
            })
        entries.sort(key=lambda entry: (entry["kind"] != "directory", str(entry["name"]).casefold()))
        return {"folder_id": folder_id, "entries": entries}


    def gmail_search(self, query: str = "is:unread", max_results: int = 20) -> object:
        arguments = [
            "gmail", "+triage",
            "--query", query or "is:unread",
            "--max", str(max(1, min(max_results, 100))),
            "--format", "json",
        ]
        return self._run_any(*arguments)

    def gmail_read(self, message_id: str) -> object:
        if not message_id:
            raise ValueError("A Gmail message ID is required")
        return self._run_any("gmail", "+read", "--id", message_id, "--headers", "--format", "json")

    def gmail_send(self, *, to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> object:
        if not to.strip():
            raise ValueError("At least one recipient is required")
        arguments = ["gmail", "+send", "--to", to, "--subject", subject, "--body", body, "--format", "json"]
        if cc.strip():
            arguments.extend(["--cc", cc])
        if bcc.strip():
            arguments.extend(["--bcc", bcc])
        return self._run_any(*arguments)

    def _run_any(self, *arguments: str) -> object:
        env = {key: value for key, value in os.environ.items() if key not in {"GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"}}
        if self.credentials_file is not None:
            env["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = str(self.credentials_file)
        env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(self.config_dir)
        env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
        completed = subprocess.run(
            [str(self.command), *arguments],
            cwd=self.workspace_dir, env=env, capture_output=True, text=True, timeout=45, check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "Google Workspace command failed"
            raise RuntimeError(detail)
        stdout = completed.stdout.strip()
        if not stdout:
            return {}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {"text": stdout}

    def _run(self, *arguments: str) -> dict[str, object]:
        env = {key: value for key, value in os.environ.items() if key not in {"GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"}}
        if self.credentials_file is not None:
            env["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = str(self.credentials_file)
        env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(self.config_dir)
        env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
        completed = subprocess.run(
            [str(self.command), *arguments],
            cwd=self.workspace_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "Google Workspace command failed"
            raise RuntimeError(detail)
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Google Workspace returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TypeError("Google Workspace returned an unexpected response")
        if payload.get("error"):
            raise RuntimeError(json.dumps(payload["error"], ensure_ascii=False))
        return payload
