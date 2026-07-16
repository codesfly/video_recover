from __future__ import annotations

from dataclasses import dataclass

import httpx

from video_recover.domain import Segment


@dataclass(frozen=True, slots=True)
class LeasePayload:
    lease_id: str
    task_id: str
    media_path: str


class WorkerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=30)
        self.headers = {"Authorization": f"Bearer {token}"}

    def lease(self, worker_id: str) -> LeasePayload | None:
        response = self.client.post(
            f"{self.base_url}/internal/worker/lease",
            headers=self.headers,
            json={"worker_id": worker_id},
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        payload = response.json()
        return LeasePayload(
            lease_id=payload["lease_id"],
            task_id=payload["task_id"],
            media_path=payload["media_path"],
        )

    def heartbeat(self, lease_id: str) -> bool:
        response = self.client.post(
            f"{self.base_url}/internal/worker/{lease_id}/heartbeat",
            headers=self.headers,
        )
        return response.status_code == 204

    def complete(self, lease_id: str, segments: list[Segment]) -> None:
        response = self.client.post(
            f"{self.base_url}/internal/worker/{lease_id}/complete",
            headers=self.headers,
            json={
                "segments": [
                    {"start": segment.start, "end": segment.end, "text": segment.text}
                    for segment in segments
                ]
            },
        )
        response.raise_for_status()

    def fail(self, lease_id: str, message: str) -> None:
        response = self.client.post(
            f"{self.base_url}/internal/worker/{lease_id}/fail",
            headers=self.headers,
            json={"message": message},
        )
        response.raise_for_status()

