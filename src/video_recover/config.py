from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="VIDEO_RECOVER_",
        extra="ignore",
    )

    data_dir: Path = Path("/data")
    host: str = "0.0.0.0"
    port: int = 8787
    log_level: str = "INFO"
    worker_token: str = Field(default="development-worker-token", min_length=16)
    native_worker_timeout_seconds: int = Field(default=300, ge=30)
    model_idle_seconds: int = Field(default=600, ge=60)
    cpu_fallback_enabled: bool = True
    max_download_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)
    minimum_free_bytes: int = Field(default=512 * 1024 * 1024, ge=1)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "db" / "video_recover.sqlite3"

    @property
    def download_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secrets" / "app.key"

    def ensure_directories(self) -> None:
        for path in (
            self.database_path.parent,
            self.download_dir,
            self.cache_dir,
            self.secret_key_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

