from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_missing_api_key_rejected() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/uploads/init", json={"file_name": "a.bin", "file_size": 4, "chunk_size": 4})
        assert response.status_code == 401
        payload = response.json()
        assert payload["error_code"] == "missing_api_key"
        assert payload["request_id"]
        assert "trace_id" in payload


def test_upload_of_another_user_is_not_found() -> None:
    old_mapping = settings.api_key_mappings
    settings.api_key_mappings = "key-a:user-a,key-b:user-b"
    try:
        with TestClient(app) as client:
            init = client.post(
                "/v1/uploads/init",
                json={"file_name": "a.bin", "file_size": 4, "chunk_size": 4},
                headers={"X-API-Key": "key-a"},
            )
            assert init.status_code == 201
            upload_id = init.json()["upload_id"]

            hidden = client.get(
                f"/v1/uploads/{upload_id}/missing-chunks",
                headers={"X-API-Key": "key-b"},
            )
            assert hidden.status_code == 404  # same answer as a missing upload
    finally:
        settings.api_key_mappings = old_mapping
