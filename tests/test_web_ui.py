from fastapi.testclient import TestClient

from app.main import app


def test_web_console_route_available() -> None:
    with TestClient(app) as client:
        response = client.get("/ui")
        assert response.status_code == 200
        assert "Distributed File Service Console" in response.text
        assert "Start Upload" in response.text
