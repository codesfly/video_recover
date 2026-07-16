from fastapi.testclient import TestClient

from video_recover.main import create_app


def test_health_reports_storage_and_service(tmp_settings):
    with TestClient(create_app(tmp_settings, start_runner=False)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "storage": "ok"}
