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

    def calendar_agenda(self, *, days: int = 7, calendar: str = "", timezone: str = "") -> object:
        arguments = ["calendar", "+agenda", "--days", str(max(1, min(days, 31))), "--format", "json"]
        if calendar.strip():
            arguments.extend(["--calendar", calendar])
        if timezone.strip():
            arguments.extend(["--timezone", timezone])
        return self._run_any(*arguments)

    def calendar_event_get(self, event_id: str, calendar_id: str = "primary") -> object:
        if not event_id.strip():
            raise ValueError("A calendar event ID is required")
        params = {"calendarId": calendar_id or "primary", "eventId": event_id}
        return self._run_any("calendar", "events", "get", "--params", json.dumps(params, separators=(",", ":")), "--format", "json")

    def calendar_freebusy(self, *, time_min: str, time_max: str, calendar_ids: list[str] | None = None, timezone: str = "") -> object:
        if not time_min.strip() or not time_max.strip():
            raise ValueError("time_min and time_max are required")
        body: dict[str, object] = {"timeMin": time_min, "timeMax": time_max, "items": [{"id": item} for item in (calendar_ids or ["primary"])]}
        if timezone.strip():
            body["timeZone"] = timezone
        return self._run_any("calendar", "freebusy", "query", "--json", json.dumps(body, separators=(",", ":")), "--format", "json")

    def calendar_event_create(self, *, summary: str, start: str, end: str, calendar_id: str = "primary", location: str = "", description: str = "", attendees: list[str] | None = None, meet: bool = False) -> object:
        if not summary.strip() or not start.strip() or not end.strip():
            raise ValueError("summary, start, and end are required")
        arguments = ["calendar", "+insert", "--calendar", calendar_id or "primary", "--summary", summary, "--start", start, "--end", end, "--format", "json"]
        if location.strip():
            arguments.extend(["--location", location])
        if description.strip():
            arguments.extend(["--description", description])
        for attendee in attendees or []:
            if attendee.strip():
                arguments.extend(["--attendee", attendee])
        if meet:
            arguments.append("--meet")
        return self._run_any(*arguments)

    def calendar_event_update(self, *, event_id: str, calendar_id: str = "primary", changes: dict[str, object]) -> object:
        if not event_id.strip():
            raise ValueError("A calendar event ID is required")
        if not changes:
            raise ValueError("At least one calendar event change is required")
        params = {"calendarId": calendar_id or "primary", "eventId": event_id, "sendUpdates": "all"}
        return self._run_any("calendar", "events", "patch", "--params", json.dumps(params, separators=(",", ":")), "--json", json.dumps(changes, separators=(",", ":")), "--format", "json")

    def calendar_event_delete(self, *, event_id: str, calendar_id: str = "primary") -> object:
        if not event_id.strip():
            raise ValueError("A calendar event ID is required")
        params = {"calendarId": calendar_id or "primary", "eventId": event_id, "sendUpdates": "all"}
        return self._run_any("calendar", "events", "delete", "--params", json.dumps(params, separators=(",", ":")), "--format", "json")

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
