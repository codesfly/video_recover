from __future__ import annotations

import gc
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from video_recover.domain import Segment
from video_recover.errors import TranscriptionFailed


class Engine(Protocol):
    def transcribe(self, path: Path) -> dict[str, Any]: ...


class MlxWhisperEngine:
    def __init__(self, model_name: str) -> None:
        import mlx_whisper

        self.mlx_whisper = mlx_whisper
        self.model_name = model_name

    def transcribe(self, path: Path) -> dict[str, Any]:
        return self.mlx_whisper.transcribe(
            str(path),
            path_or_hf_repo=self.model_name,
            language="zh",
            word_timestamps=True,
        )


def _load_engine(model_name: str) -> Engine:
    return MlxWhisperEngine(model_name)


def _now() -> datetime:
    return datetime.now(UTC)


class LazyMlxTranscriber:
    def __init__(
        self,
        *,
        model_name: str = "mlx-community/whisper-large-v3-turbo",
        loader: Callable[[str], Engine] = _load_engine,
        idle_seconds: int = 600,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.model_name = model_name
        self.loader = loader
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._engine: Engine | None = None
        self._last_used: datetime | None = None

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    def transcribe(self, video_path: Path) -> list[Segment]:
        try:
            if self._engine is None:
                self._engine = self.loader(self.model_name)
            result = self._engine.transcribe(video_path)
            segments = [
                Segment(float(item["start"]), float(item["end"]), str(item["text"]).strip())
                for item in result.get("segments", [])
                if str(item.get("text", "")).strip()
            ]
            self._last_used = self.clock()
            return segments
        except TranscriptionFailed:
            raise
        except Exception as exc:
            raise TranscriptionFailed("Mac 原生 MLX 转写失败") from exc

    def unload_if_idle(self) -> bool:
        if self._engine is None or self._last_used is None:
            return False
        idle = (self.clock() - self._last_used).total_seconds()
        if idle <= self.idle_seconds:
            return False
        close = getattr(self._engine, "close", None)
        if callable(close):
            close()
        self._engine = None
        self._last_used = None
        gc.collect()
        return True

