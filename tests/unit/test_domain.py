import pytest

from video_recover.domain import TaskStatus, require_transition
from video_recover.errors import InvalidTransition


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

