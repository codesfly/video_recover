from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from video_recover.domain import Task
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


def build_mcp(service: VideoService) -> FastMCP:
    mcp = FastMCP(
        "video-recover",
        instructions=(
            "Video downloads and transcription are asynchronous. Call submit_video, "
            "then poll get_task until completed or partial before reading artifacts."
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
    def submit_video(url: str, transcribe: bool = True) -> dict[str, Any]:
        """Submit one public Douyin video URL for local download and transcription."""
        try:
            task, created = service.submit(url, source="mcp", transcribe=transcribe)
        except UserFacingError as exc:
            return {"ok": False, "error_code": exc.code, "message": exc.message}
        payload = _task_payload(task)
        payload.update({"ok": True, "created": created})
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
