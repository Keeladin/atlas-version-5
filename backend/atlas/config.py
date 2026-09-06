from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_STATE = Path.home() / ".local" / "state" / "atlas-v5-dev"
_DEV_CONFIG = Path.home() / ".config" / "atlas-v5-dev"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_", extra="ignore")

    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8086
    database_url: str | None = "postgresql+psycopg://atlas_v5@127.0.0.1/atlas_v5"
    database_url_file: Path | None = None
    state_dir: Path = _DEV_STATE
    artifact_dir: Path = _DEV_STATE / "artifacts"
    secret_dir: Path = _DEV_CONFIG / "secrets"
    frontend_dist: Path = Path("frontend/dist")
    workspace_root: Path = Path.home() / "Workspace"
    workspace_display_root: str = "~/Workspace"
    openai_api_key_file: Path | None = None
    openai_model: str = "gpt-5.6-sol"
    openai_context_window: int = 1_000_000
    gws_command: Path = Path("/opt/atlas-v5/bin/gws")
    gws_credentials_file: Path | None = None
    gws_config_dir: Path = _DEV_STATE / "google-workspace" / "config"
    gws_workspace_dir: Path = _DEV_STATE / "google-workspace" / "workspace"
    github_mcp_command: Path = Path("/opt/atlas-v5/bin/github-mcp-server")
    github_token_file: Path | None = None
    github_mcp_toolsets: str = "repos,git,pull_requests,issues"

    @property
    def database_dsn(self) -> str:
        if self.database_url_file is not None:
            return self.database_url_file.read_text().strip()
        if self.database_url is None:
            raise RuntimeError("No Atlas database URL is configured")
        return self.database_url

    @property
    def openai_api_key(self) -> str | None:
        if self.openai_api_key_file is None:
            return None
        key = self.openai_api_key_file.read_text().strip()
        return key or None

    @property
    def gws_configured(self) -> bool:
        encrypted = self.gws_config_dir / "credentials.enc"
        return self.gws_command.is_file() and (
            encrypted.is_file()
            or (self.gws_credentials_file is not None and self.gws_credentials_file.is_file())
        )

    @property
    def github_configured(self) -> bool:
        return (
            self.github_mcp_command.is_file()
            and self.github_token_file is not None
            and self.github_token_file.is_file()
        )

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
