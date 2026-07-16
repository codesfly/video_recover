from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from video_recover.domain import TaskStatus
from video_recover.errors import InvalidTransition
from video_recover.repository import Repository

TEST_URL = "https://www.douyin.com/video/7662212894569811235"


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def repository(tmp_path, clock) -> Repository:
    return Repository(tmp_path / "video_recover.sqlite3", clock=clock.now)


def test_create_task_deduplicates_canonical_url(repository):
    first, created = repository.create_or_get_task(TEST_URL)
    second, created_again = repository.create_or_get_task(TEST_URL)

    assert created is True
    assert created_again is False
    assert first.id == second.id
    assert first.status == TaskStatus.QUEUED


def test_task_persists_after_repository_reopens(tmp_path, clock):
    database_path = tmp_path / "video_recover.sqlite3"
    first_repository = Repository(database_path, clock=clock.now)
    task, _ = first_repository.create_or_get_task(TEST_URL)

    reopened = Repository(database_path, clock=clock.now)
    assert reopened.get_task(task.id).canonical_url == TEST_URL


def test_transition_updates_task_and_records_event(repository):
    task, _ = repository.create_or_get_task(TEST_URL)
    updated = repository.transition(
        task.id,
        TaskStatus.RESOLVING,
        progress=5,
        message="正在解析",
    )

    assert updated.status == TaskStatus.RESOLVING
    assert updated.progress == 5
    assert [event.status for event in repository.list_events(task.id)] == [
        TaskStatus.QUEUED,
        TaskStatus.RESOLVING,
    ]


def test_invalid_transition_does_not_change_database(repository):
    task, _ = repository.create_or_get_task(TEST_URL)

    with pytest.raises(InvalidTransition):
        repository.transition(
            task.id,
            TaskStatus.COMPLETED,
            progress=100,
            message="错误完成",
        )

    assert repository.get_task(task.id).status == TaskStatus.QUEUED


def test_expired_transcription_lease_returns_to_queue(repository, clock):
    task, _ = repository.create_or_get_task(TEST_URL)
    repository.transition(task.id, TaskStatus.RESOLVING, progress=5, message="解析")
    repository.transition(task.id, TaskStatus.DOWNLOADING, progress=20, message="下载")
    repository.transition(
        task.id,
        TaskStatus.AWAITING_TRANSCRIPTION,
        progress=60,
        message="等待转写",
    )

    lease = repository.acquire_transcription_lease("worker-a", ttl_seconds=30)
    assert lease is not None
    assert repository.get_task(task.id).status == TaskStatus.TRANSCRIBING

    clock.advance(seconds=31)
    assert repository.recover_expired_leases() == 1
    assert repository.get_task(task.id).status == TaskStatus.AWAITING_TRANSCRIPTION
