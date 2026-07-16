from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

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
    TaskStatus.PARTIAL: frozenset({TaskStatus.AWAITING_TRANSCRIPTION, TaskStatus.CANCELLED}),
    TaskStatus.FAILED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
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

