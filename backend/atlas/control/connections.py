"""Owner-managed connection configuration stored outside source and secrets UI."""
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def connection_paths(settings) -> dict[str, Path]:
    root = settings.state_dir / "control"
    return {
        "root": root,
        "secrets": root / "secrets",
        "metadata": root / "connections.json",
        "openai": root / "secrets" / "openai-api-key",
        "github": root / "secrets" / "github-token",
        "google": root / "secrets" / "google-workspace-authorized-user.json",
        "google_config": root / "google-workspace-config",
    }


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    _ensure_private_dir(path.parent)
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    temp_path.chmod(mode)
    os.replace(temp_path, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def _metadata_lock(paths: dict[str, Path]):
    _ensure_private_dir(paths["root"])
    lock_path = paths["root"] / "connections.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    _atomic_write(path, (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode())


def apply_managed_overrides(settings):
    paths = connection_paths(settings)
    metadata = _read_metadata(paths["metadata"])
    if paths["openai"].is_file() and not paths["openai"].is_symlink():
        settings.openai_api_key_file = paths["openai"]
    if paths["github"].is_file() and not paths["github"].is_symlink():
        settings.github_token_file = paths["github"]
    if paths["google"].is_file() and not paths["google"].is_symlink():
        settings.gws_credentials_file = paths["google"]
        settings.gws_config_dir = paths["google_config"]
    model = metadata.get("openai_model")
    owner = metadata.get("github_owner")
    if isinstance(model, str) and model.strip():
        settings.openai_model = model.strip()
    if isinstance(owner, str) and owner.strip():
        settings.github_owner = owner.strip()
    return settings


def save_model_connection(settings, api_key: str, model: str) -> None:
    api_key = api_key.strip()
    model = model.strip()
    if len(api_key) < 20:
        raise ValueError("Model API credential is too short")
    if not model:
        raise ValueError("Model name is required")
    paths = connection_paths(settings)
    _atomic_write(paths["openai"], (api_key + "\n").encode())
    with _metadata_lock(paths):
        metadata = _read_metadata(paths["metadata"])
        metadata["openai_model"] = model
        _write_metadata(paths["metadata"], metadata)
    settings.openai_api_key_file = paths["openai"]
    settings.openai_model = model


def save_github_connection(settings, token: str, owner: str) -> None:
    token = token.strip()
    owner = owner.strip()
    if len(token) < 20:
        raise ValueError("GitHub token is too short")
    if not owner or any(character.isspace() for character in owner):
        raise ValueError("GitHub owner is required")
    paths = connection_paths(settings)
    _atomic_write(paths["github"], (token + "\n").encode())
    with _metadata_lock(paths):
        metadata = _read_metadata(paths["metadata"])
        metadata["github_owner"] = owner
        _write_metadata(paths["metadata"], metadata)
    settings.github_token_file = paths["github"]
    settings.github_owner = owner


def save_google_connection(settings, credentials: dict[str, Any]) -> None:
    required = {"client_id", "client_secret", "refresh_token"}
    if credentials.get("type") != "authorized_user" or not required.issubset(credentials):
        raise ValueError("Google credential must be an authorized_user JSON with client_id, client_secret and refresh_token")
    if not all(isinstance(credentials.get(key), str) and credentials[key].strip() for key in required):
        raise ValueError("Google authorized_user credential contains empty required fields")
    paths = connection_paths(settings)
    safe = {
        "type": "authorized_user",
        "client_id": credentials["client_id"].strip(),
        "client_secret": credentials["client_secret"].strip(),
        "refresh_token": credentials["refresh_token"].strip(),
    }
    if isinstance(credentials.get("quota_project_id"), str) and credentials["quota_project_id"].strip():
        safe["quota_project_id"] = credentials["quota_project_id"].strip()
    _ensure_private_dir(paths["google_config"])
    _atomic_write(paths["google"], (json.dumps(safe, separators=(",", ":")) + "\n").encode())
    settings.gws_credentials_file = paths["google"]
    settings.gws_config_dir = paths["google_config"]
