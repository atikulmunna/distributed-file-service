from fastapi.testclient import TestClient

from app.main import app


def test_web_console_route_available() -> None:
    with TestClient(app) as client:
        response = client.get("/ui")
        assert response.status_code == 200
        assert "Distributed File Service Console" in response.text
        assert "Start Upload" in response.text


def test_console_alias_route_available() -> None:
    with TestClient(app) as client:
        response = client.get("/console")
        assert response.status_code == 200
        assert "Distributed File Service Console" in response.text


def test_version_route_available() -> None:
    with TestClient(app) as client:
        response = client.get("/version")
        assert response.status_code == 200
        payload = response.json()
        assert "app_name" in payload
        assert "app_version" in payload
        assert "queue_backend" in payload
        assert "storage_backend" in payload


def test_app_version_header_present() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.headers.get("X-DFS-App-Version") is not None
