from fastapi.testclient import TestClient

from video_recover.main import create_app
from video_recover.runner import JobRunner


def test_health_reports_storage_and_service(tmp_settings):
    with TestClient(create_app(tmp_settings, start_runner=False)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "storage": "ok"}


def test_lifespan_recovers_pipeline_before_starting_runner(tmp_settings, monkeypatch):
    calls = []

    class FakeThread:
        def join(self, *, timeout):
            calls.append(("join", timeout))

    monkeypatch.setattr(
        JobRunner,
        "recover_startup",
        lambda _self: calls.append("recover"),
        raising=False,
    )
    monkeypatch.setattr(
        JobRunner,
        "start_thread",
        lambda _self: calls.append("start") or FakeThread(),
    )
    monkeypatch.setattr(JobRunner, "stop", lambda _self: calls.append("stop"))

    with TestClient(create_app(tmp_settings)) as client:
        assert client.get("/healthz").status_code == 200

    assert calls[:2] == ["recover", "start"]
    assert calls[-2:] == ["stop", ("join", 5)]
