from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from video_recover.domain import Segment


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    txt: Path
    srt: Path
    markdown: Path


def render_txt(segments: list[Segment]) -> str:
    lines = [segment.text.strip() for segment in segments if segment.text.strip()]
    return "\n".join(lines) + ("\n" if lines else "")


def _srt_timestamp(seconds: float) -> str:
    total_milliseconds = int(seconds * 1000 + 0.5)
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"


def render_srt(segments: list[Segment]) -> str:
    blocks = []
    for index, segment in enumerate(segments, start=1):
        blocks.append(
            f"{index}\n{_srt_timestamp(segment.start)} --> {_srt_timestamp(segment.end)}\n"
            f"{segment.text.strip()}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _paragraphs(
    segments: list[Segment],
    *,
    silence_seconds: float = 1.2,
    maximum_characters: int = 180,
) -> list[str]:
    paragraphs: list[str] = []
    current = ""
    for index, segment in enumerate(segments):
        current += segment.text.strip()
        is_last = index == len(segments) - 1
        gap = 0.0 if is_last else segments[index + 1].start - segment.end
        if is_last or gap >= silence_seconds or len(current) >= maximum_characters:
            paragraphs.append(current)
            current = ""
    return paragraphs


def render_markdown(description: str, segments: list[Segment]) -> str:
    clean_description = description.strip() or "（无发布描述）"
    body = "\n\n".join(_paragraphs(segments)) or "（未识别到语音）"
    return f"# 视频归档\n\n## 发布描述\n\n{clean_description}\n\n## 视频文案\n\n{body}\n"


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    file: TextIO
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def write_artifacts(
    output_dir: Path,
    description: str,
    segments: list[Segment],
) -> ArtifactPaths:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = ArtifactPaths(
        txt=output_dir / "transcript.txt",
        srt=output_dir / "transcript.srt",
        markdown=output_dir / "transcript.md",
    )
    _atomic_write(paths.txt, render_txt(segments))
    _atomic_write(paths.srt, render_srt(segments))
    _atomic_write(paths.markdown, render_markdown(description, segments))
    return paths
