from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from video_recover.domain import ACTIVE_COMMENT_STATUSES, CommentJob, Task
from video_recover.errors import InvalidTransition, UserFacingError
from video_recover.service import VideoService


def _task_payload(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "canonical_url": task.canonical_url,
        "status": task.status.value,
        "progress": task.progress,
        "message": task.message,
        "transcribe": task.transcribe,
        "aweme_id": task.aweme_id,
        "error_code": task.error_code,
        "error_message": task.error_message,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def _comment_job_payload(job: CommentJob) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "status": job.status.value,
        "max_comments": job.max_comments,
        "fetched_count": job.fetched_count,
        "has_more_remaining": job.has_more_remaining,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def build_mcp(service: VideoService) -> FastMCP:
    mcp = FastMCP(
        "video-recover",
        instructions=(
            "Video downloads and transcription are asynchronous. Call submit_video, "
            "then poll get_task until completed or partial before reading artifacts. "
            "Comment snapshots are also asynchronous: call fetch_comments, then poll "
            "get_comments. Only top-level comments are collected; replies are not."
        ),
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*", "localhost", "testserver"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
        ),
    )

    @mcp.tool()
    def submit_video(
        url: str, transcribe: bool = True, fetch_comments: bool = False
    ) -> dict[str, Any]:
        """Submit one public Douyin video URL for local download and transcription.
        Set fetch_comments to also queue a top-level comment snapshot."""
        try:
            task, created = service.submit(
                url, source="mcp", transcribe=transcribe, fetch_comments=fetch_comments
            )
        except UserFacingError as exc:
            return {"ok": False, "error_code": exc.code, "message": exc.message}
        active = [
            job
            for job in service.repository.list_comment_jobs(task.id)
            if job.status in ACTIVE_COMMENT_STATUSES
        ]
        payload = _task_payload(task)
        payload.update(
            {"ok": True, "created": created, "comment_job_id": active[0].id if active else None}
        )
        return payload

    @mcp.tool()
    def get_task(task_id: str) -> dict[str, Any]:
        """Read the current state and progress of one recovery task."""
        try:
            return {"ok": True, **_task_payload(service.repository.get_task(task_id))}
        except KeyError:
            return {"ok": False, "error_code": "not_found", "message": "任务不存在"}

    @mcp.tool()
    def list_videos(limit: int = 20) -> dict[str, Any]:
        """List recent local video recovery tasks."""
        safe_limit = min(max(limit, 1), 100)
        return {
            "ok": True,
            "tasks": [
                _task_payload(task)
                for task in service.repository.list_tasks(limit=safe_limit)
            ],
        }

    @mcp.tool()
    def get_metadata(task_id: str) -> dict[str, Any]:
        """Read parsed Douyin metadata and the original post description."""
        try:
            task = service.repository.get_task(task_id)
        except KeyError:
            return {"ok": False, "error_code": "not_found", "message": "任务不存在"}
        if task.metadata_json is None:
            return {
                "ok": True,
                "available": False,
                "status": task.status.value,
                "message": "元数据尚未生成",
            }
        metadata = json.loads(task.metadata_json)
        return {"ok": True, "available": True, **metadata}

    @mcp.tool()
    def get_transcript(task_id: str) -> dict[str, Any]:
        """Read the extracted speech transcript when it is available."""
        try:
            task = service.repository.get_task(task_id)
        except KeyError:
            return {"ok": False, "error_code": "not_found", "message": "任务不存在"}
        if task.output_dir is None:
            return {
                "ok": True,
                "available": False,
                "status": task.status.value,
                "message": "转写文案尚未生成",
            }
        transcript_path = task.output_dir / "transcript.txt"
        if not transcript_path.is_file():
            return {
                "ok": True,
                "available": False,
                "status": task.status.value,
                "message": "转写文案尚未生成",
            }
        return {
            "ok": True,
            "available": True,
            "status": task.status.value,
            "transcript": transcript_path.read_text(encoding="utf-8"),
        }

    @mcp.tool()
    def retry_task(task_id: str) -> dict[str, Any]:
        """Retry a failed or partial task using its persisted inputs."""
        try:
            return {"ok": True, **_task_payload(service.retry(task_id))}
        except KeyError:
            return {"ok": False, "error_code": "not_found", "message": "任务不存在"}
        except InvalidTransition:
            return {
                "ok": False,
                "error_code": "invalid_state",
                "message": "当前任务状态不能重试",
            }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    def fetch_comments(task_id: str, max_comments: int = 200) -> dict[str, Any]:
        """Queue an anonymous snapshot of a video's top-level comments (replies are not
        collected). Asynchronous: poll get_comments until a snapshot is available."""
        try:
            job, created = service.request_comments(task_id, max_comments=max_comments)
        except KeyError:
            return {"ok": False, "error_code": "not_found", "message": "任务不存在"}
        except InvalidTransition:
            return {
                "ok": False,
                "error_code": "invalid_state",
                "message": "已取消的任务不能拉取评论",
            }
        except ValueError:
            return {
                "ok": False,
                "error_code": "invalid_limit",
                "message": "max_comments 需在 1–1000 之间",
            }
        return {"ok": True, "created": created, **_comment_job_payload(job)}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_comments(
        task_id: str, job_id: str | None = None, sort: str = "likes", limit: int = 50
    ) -> dict[str, Any]:
        """Read a comment snapshot as compact text, one comment per line
        (likes, replies, date, IP region, text). Defaults to the latest snapshot."""
        safe_limit = max(1, min(limit, 200))
        try:
            view = service.read_comments(
                task_id,
                job_id=job_id,
                sort="time" if sort == "time" else "likes",
                limit=safe_limit,
            )
        except KeyError:
            return {"ok": False, "error_code": "not_found", "message": "任务或快照不存在"}
        if view is None:
            jobs = service.repository.list_comment_jobs(task_id)
            return {
                "ok": True,
                "available": False,
                "latest_job": _comment_job_payload(jobs[0]) if jobs else None,
                "message": "还没有评论快照，可先调用 fetch_comments",
            }
        lines = [
            " · ".join(
                [
                    f"👍{comment.digg_count} 💬{comment.reply_count}",
                    f"{comment.created_at:%Y-%m-%d}" if comment.created_at else "—",
                    comment.ip_label or "—",
                    " ".join(comment.text.split()),
                ]
            )
            for comment in view.comments
        ]
        return {
            "ok": True,
            "available": True,
            "limit": safe_limit,
            "snapshot": {
                **_comment_job_payload(view.job),
                "fetched_at": view.fetched_at,
                "total_in_snapshot": view.total_in_snapshot,
            },
            "text": "\n".join(lines),
        }

    @mcp.tool()
    def get_service_status() -> dict[str, Any]:
        """Check local service availability, Cookie configuration and queue size."""
        tasks = service.repository.list_tasks(limit=500)
        return {
            "ok": True,
            "status": "ready",
            "cookie_configured": service.cookie_configured(),
            "task_count": len(tasks),
            "active_count": sum(
                task.status.value
                in {
                    "queued",
                    "resolving",
                    "downloading",
                    "awaiting_transcription",
                    "transcribing",
                }
                for task in tasks
            ),
        }

    return mcp
