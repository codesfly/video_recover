from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from video_recover.errors import InvalidTransition


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    AWAITING_TRANSCRIPTION = "awaiting_transcription"
    TRANSCRIBING = "transcribing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RESOLVING, TaskStatus.CANCELLED}),
    TaskStatus.RESOLVING: frozenset(
        {TaskStatus.DOWNLOADING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.DOWNLOADING: frozenset(
        {
            TaskStatus.AWAITING_TRANSCRIPTION,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.AWAITING_TRANSCRIPTION: frozenset(
        {TaskStatus.TRANSCRIBING, TaskStatus.PARTIAL, TaskStatus.CANCELLED}
    ),
    TaskStatus.TRANSCRIBING: frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.PARTIAL,
            TaskStatus.AWAITING_TRANSCRIPTION,
        }
    ),
    TaskStatus.PARTIAL: frozenset(
        {TaskStatus.RESOLVING, TaskStatus.AWAITING_TRANSCRIPTION, TaskStatus.CANCELLED}
    ),
    TaskStatus.FAILED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.RESOLVING, TaskStatus.CANCELLED}
    ),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.COMPLETED: frozenset(),
}


def require_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"cannot transition task from {current.value} to {target.value}")


@dataclass(frozen=True, slots=True)
class Segment:
    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("segment timestamps must be monotonic and non-negative")
        if not self.text.strip():
            raise ValueError("segment text cannot be empty")


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    canonical_url: str
    original_url: str
    status: TaskStatus
    progress: int
    message: str
    source: str
    transcribe: bool
    aweme_id: str | None
    output_dir: Path | None
    metadata_json: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskEvent:
    id: int
    task_id: str
    status: TaskStatus
    message: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TranscriptionLease:
    id: str
    task_id: str
    worker_id: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
