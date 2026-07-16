from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MacWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="VIDEO_RECOVER_",
        extra="ignore",
    )

    control_url: str = "http://127.0.0.1:8787"
    worker_token: str = Field(min_length=16)
    data_dir: Path
    worker_id: str = "mac-mlx"
    poll_seconds: float = Field(default=2.0, ge=0.2)
    heartbeat_seconds: float = Field(default=20.0, ge=1)
    model_idle_seconds: int = Field(default=600, ge=60)
    mlx_model: str = "mlx-community/whisper-large-v3-turbo"

