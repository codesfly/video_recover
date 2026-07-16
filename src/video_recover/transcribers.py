from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from video_recover.domain import Segment
from video_recover.errors import TranscriptionFailed


class Transcriber(Protocol):
    def transcribe(self, video_path: Path) -> list[Segment]: ...


class CpuTranscriber:
    def __init__(
        self,
        *,
        model_name: str = "small",
        model_loader: Callable[..., Any] | None = None,
    ) -> None:
        self.model_name = model_name
        self.model_loader = model_loader
        self._model: Any | None = None

    def _load_model(self):
        if self._model is None:
            loader = self.model_loader
            if loader is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise TranscriptionFailed("容器未安装 CPU 转写组件") from exc
                loader = WhisperModel
            self._model = loader(self.model_name, device="cpu", compute_type="int8")
        return self._model

    def transcribe(self, video_path: Path) -> list[Segment]:
        try:
            raw_segments, _info = self._load_model().transcribe(
                str(video_path),
                language="zh",
                vad_filter=True,
                beam_size=5,
            )
            return [
                Segment(float(segment.start), float(segment.end), str(segment.text).strip())
                for segment in raw_segments
                if str(segment.text).strip()
            ]
        except TranscriptionFailed:
            raise
        except Exception as exc:
            raise TranscriptionFailed() from exc

