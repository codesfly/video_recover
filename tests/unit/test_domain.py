import pytest

from video_recover.domain import (
    ACTIVE_COMMENT_STATUSES,
    CommentJobStatus,
    TaskStatus,
    require_transition,
)
from video_recover.errors import (
    CommentFetchStalled,
    CommentRateLimited,
    CommentSignMissing,
    CommentsUnavailable,
    InvalidTransition,
)


def test_pipeline_allows_expected_forward_transitions():
    require_transition(TaskStatus.QUEUED, TaskStatus.RESOLVING)
    require_transition(TaskStatus.RESOLVING, TaskStatus.DOWNLOADING)
    require_transition(TaskStatus.DOWNLOADING, TaskStatus.AWAITING_TRANSCRIPTION)
    require_transition(TaskStatus.AWAITING_TRANSCRIPTION, TaskStatus.TRANSCRIBING)
    require_transition(TaskStatus.TRANSCRIBING, TaskStatus.COMPLETED)


def test_completed_task_cannot_return_to_downloading():
    with pytest.raises(InvalidTransition):
        require_transition(TaskStatus.COMPLETED, TaskStatus.DOWNLOADING)


def test_failed_task_can_be_requeued_for_retry():
    require_transition(TaskStatus.FAILED, TaskStatus.QUEUED)


def test_comment_job_statuses_and_active_set() -> None:
    assert {status.value for status in CommentJobStatus} == {
        "queued", "running", "completed", "partial", "failed",
    }
    assert {CommentJobStatus.QUEUED, CommentJobStatus.RUNNING} == ACTIVE_COMMENT_STATUSES


def test_comment_errors_have_stable_codes_and_retry_flags() -> None:
    assert (CommentsUnavailable().code, CommentsUnavailable().retryable) == (
        "comments_unavailable", False,
    )
    assert CommentSignMissing().code == "comment_sign_missing"
    assert CommentFetchStalled().code == "comment_fetch_stalled"
    assert CommentRateLimited().code == "comment_rate_limited"
    assert all(e().retryable for e in (CommentSignMissing, CommentFetchStalled, CommentRateLimited))
