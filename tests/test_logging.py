import json

from fastapi.testclient import TestClient

from app.main import app


def _events_from_caplog(caplog) -> list[dict]:
    events: list[dict] = []
    for record in caplog.records:
        if record.name != "dfs.request":
            continue
        try:
            events.append(json.loads(record.message))
        except json.JSONDecodeError:
            continue
    return events


def test_request_completed_log_contains_request_id(caplog) -> None:
    caplog.set_level("INFO", logger="dfs.request")
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "req-123"})
        assert response.status_code == 200
        assert response.headers.get("X-Request-ID") == "req-123"

    events = _events_from_caplog(caplog)
    completed = [e for e in events if e.get("event") == "request_completed" and e.get("path") == "/health"]
    assert completed
    assert completed[-1]["request_id"] == "req-123"
    assert completed[-1]["status_code"] == 200
    assert "trace_id" in completed[-1]


def test_request_error_log_contains_upload_and_error_class(caplog) -> None:
    caplog.set_level("INFO", logger="dfs.request")
    with TestClient(app) as client:
        response = client.post("/v1/uploads/not-found/complete", headers={"X-API-Key": "dev-key"})
        assert response.status_code == 404

    events = _events_from_caplog(caplog)
    errors = [e for e in events if e.get("event") == "request_error" and e.get("path").endswith("/complete")]
    assert errors
    assert errors[-1]["upload_id"] == "not-found"
    assert errors[-1]["error_class"] == "client_error"
    assert "trace_id" in errors[-1]
