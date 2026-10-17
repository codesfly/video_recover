from __future__ import annotations

import json

import pytest

from video_recover.comments import CommentHarvest, map_comment
from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.domain import CommentJobStatus, TaskStatus
from video_recover.errors import CommentRateLimited, CommentsUnavailable, InvalidTransition
from video_recover.repository import Repository
from video_recover.service import VideoService

TEST_URL = "https://www.douyin.com/video/7662212894569811235"
AWEME_ID = "7662212894569811235"


def make_repository(tmp_path):
    repository = Repository(tmp_path / "db.sqlite3")
    task, _ = repository.create_or_get_task(TEST_URL)
    return repository, task


def test_one_active_comment_job_per_task(tmp_path) -> None:
    repository, task = make_repository(tmp_path)
    first, created = repository.create_or_get_comment_job(task.id, max_comments=200)
    again, created_again = repository.create_or_get_comment_job(task.id, max_comments=50)
    assert created and not created_again
    assert again.id == first.id and again.max_comments == 200


def test_claim_finish_and_latest_snapshot(tmp_path) -> None:
    repository, task = make_repository(tmp_path)
    job, _ = repository.create_or_get_comment_job(task.id, max_comments=200)
    claimed = repository.claim_next_comment_job()
    assert claimed is not None and claimed.status == CommentJobStatus.RUNNING
    assert repository.claim_next_comment_job() is None
    assert repository.has_running_comment_job(task.id)

    done = repository.finish_comment_job(
        job.id, status=CommentJobStatus.PARTIAL, fetched_count=41, pages=3,
        has_more_remaining=True, total_reported=1131,
        snapshot_name="20261018T031500Z-1a2b3c4d",
        error_code="comment_rate_limited", error_message="限流",
    )
    assert done.finished_at is not None and done.fetched_count == 41
    assert repository.latest_comment_snapshot_job(task.id).id == job.id
    second, created = repository.create_or_get_comment_job(task.id, max_comments=200)
    assert created and second.id != job.id
    assert [j.id for j in repository.list_comment_jobs(task.id)] == [second.id, job.id]


def test_failed_jobs_are_not_snapshots_and_restart_requeues_running(tmp_path) -> None:
    repository, task = make_repository(tmp_path)
    job, _ = repository.create_or_get_comment_job(task.id, max_comments=200)
    repository.claim_next_comment_job()
    assert repository.recover_interrupted_comment_jobs() == 1
    requeued = repository.get_comment_job(job.id)
    assert requeued.status == CommentJobStatus.QUEUED and requeued.started_at is None

    repository.claim_next_comment_job()
    repository.finish_comment_job(
        job.id, status=CommentJobStatus.FAILED, fetched_count=0, pages=0,
        has_more_remaining=False, total_reported=None, snapshot_name=None,
        error_code="comments_unavailable", error_message="无评论区",
    )
    assert repository.latest_comment_snapshot_job(task.id) is None


def test_comment_jobs_cascade_with_task(tmp_path) -> None:
    repository, task = make_repository(tmp_path)
    job, _ = repository.create_or_get_comment_job(task.id, max_comments=200)
    repository.delete_task(task.id)
    assert repository.list_comment_jobs(task.id) == []


class StubFetcher:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def fetch(self, canonical_url, *, max_comments, deadline_seconds):
        self.calls.append((canonical_url, max_comments, deadline_seconds))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def harvest(*cids, error=None, has_more=False):
    return CommentHarvest(
        comments=tuple(
            map_comment({"cid": c, "text": f"评论{c}", "digg_count": int(c), "nickname": "用户"})
            for c in cids
        ),
        pages=1,
        has_more_remaining=has_more,
        total_reported=10,
        stop_error=error,
    )


def make_service(tmp_path, fetcher=None):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_directories()
    service = VideoService(
        settings=settings,
        repository=Repository(settings.database_path),
        parser=object(),
        cookie_vault=CookieVault(settings.secret_key_path),
        comment_fetcher=fetcher,
    )
    return settings, service


def run_job(service, task_id, **kwargs):
    service.request_comments(task_id, **kwargs)
    return service.process_comment_job(service.repository.claim_next_comment_job())


def test_completed_job_writes_snapshot_and_latest_markdown(tmp_path) -> None:
    fetcher = StubFetcher(harvest("1", "5"))
    settings, service = make_service(tmp_path, fetcher)
    task, _ = service.submit(TEST_URL)

    job = run_job(service, task.id)

    assert job.status == CommentJobStatus.COMPLETED and job.fetched_count == 2
    assert fetcher.calls == [(TEST_URL, 200, 180)]
    folder = settings.download_dir / AWEME_ID / "comments"
    snapshot = json.loads((folder / f"{job.snapshot_name}.json").read_text(encoding="utf-8"))
    assert snapshot["status"] == "completed"
    assert set(snapshot["comments"][0]) == {
        "cid", "text", "digg_count", "reply_count", "created_at", "ip_label", "nickname",
    }
    assert (folder / "latest.md").read_text(encoding="utf-8").startswith("# 评论快照")


def test_partial_and_failed_outcomes(tmp_path) -> None:
    fetcher = StubFetcher(harvest("1", error=CommentRateLimited()))
    _settings, service = make_service(tmp_path, fetcher)
    task, _ = service.submit(TEST_URL)
    partial = run_job(service, task.id)
    assert (partial.status, partial.error_code) == (
        CommentJobStatus.PARTIAL, "comment_rate_limited",
    )
    assert partial.snapshot_name is not None

    service.comment_fetcher = StubFetcher(CommentsUnavailable())
    failed = run_job(service, task.id)
    assert (failed.status, failed.snapshot_name) == (CommentJobStatus.FAILED, None)
    assert service.repository.latest_comment_snapshot_job(task.id).id == partial.id


def test_read_comments_sorts_and_limits(tmp_path) -> None:
    _settings, service = make_service(tmp_path, StubFetcher(harvest("1", "9", "5")))
    task, _ = service.submit(TEST_URL)
    run_job(service, task.id)

    view = service.read_comments(task.id, sort="likes", limit=2)
    assert [c.cid for c in view.comments] == ["9", "5"]
    assert view.total_in_snapshot == 3
    assert service.read_comments(task.id, sort="time", limit=10) is not None


def test_read_comments_rejects_job_of_another_task(tmp_path) -> None:
    _settings, service = make_service(tmp_path, StubFetcher(harvest("1")))
    task, _ = service.submit(TEST_URL)
    job = run_job(service, task.id)
    other, _ = service.submit("https://www.douyin.com/video/7662212894569811236")
    with pytest.raises(KeyError):
        service.read_comments(other.id, job_id=job.id)


def test_request_rejects_cancelled_and_validates_limit(tmp_path) -> None:
    _settings, service = make_service(tmp_path, StubFetcher(harvest()))
    task, _ = service.submit(TEST_URL)
    with pytest.raises(ValueError):
        service.request_comments(task.id, max_comments=1001)
    service.repository.transition(task.id, TaskStatus.CANCELLED, progress=0, message="取消")
    with pytest.raises(InvalidTransition):
        service.request_comments(task.id)


def test_submit_can_queue_comments_and_delete_cleans_folder(tmp_path) -> None:
    settings, service = make_service(tmp_path, StubFetcher(harvest("1")))
    task, _ = service.submit(TEST_URL, fetch_comments=True)
    assert len(service.repository.list_comment_jobs(task.id)) == 1

    claimed = service.repository.claim_next_comment_job()
    with pytest.raises(InvalidTransition):
        service.delete(task.id)  # comment job is running
    service.process_comment_job(claimed)
    assert (settings.download_dir / AWEME_ID / "comments" / "latest.md").is_file()

    service.delete(task.id)
    assert not (settings.download_dir / AWEME_ID).exists()
