from __future__ import annotations

from video_recover.domain import CommentJobStatus
from video_recover.repository import Repository

TEST_URL = "https://www.douyin.com/video/7662212894569811235"


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
