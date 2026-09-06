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

    @property
    def database_dsn(self) -> str:
        if self.database_url_file is not None:
            return self.database_url_file.read_text().strip()
        if self.database_url is None:
            raise RuntimeError("No Atlas database URL is configured")
        return self.database_url

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
