"""Readiness (/ready checks dependencies) versus liveness (/health), and the instance identity."""

import json
import logging

from app.jobs import job_queue
from app.logging_utils import INSTANCE_ID
from app.storage import storage
from app.storage_policy import storage_breaker


def test_ready_ok(client) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": "ok", "storage": "ok", "queue": "ok"}
    assert body["instance_id"] == INSTANCE_ID


def test_ready_reports_storage_down(client, monkeypatch) -> None:
    def unreachable() -> None:
        raise OSError("storage root is not writable")

    monkeypatch.setattr(storage, "check", unreachable)
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not ready"
    assert body["checks"]["storage"] == "error: OSError"
    assert body["checks"]["database"] == "ok"
    assert client.get("/health").status_code == 200  # liveness is unaffected


def test_ready_reports_storage_down_while_breaker_open(client, monkeypatch) -> None:
    monkeypatch.setattr(storage_breaker, "failure_threshold", 1)
    storage_breaker.record_failure(transient=True)
    called = {"n": 0}
    monkeypatch.setattr(storage, "check", lambda: called.__setitem__("n", called["n"] + 1))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["storage"] == "down: circuit breaker open"
    assert called["n"] == 0  # no probe of storage while the breaker is open


def test_ready_reports_queue_down(client, monkeypatch) -> None:
    def broken() -> bool:
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(job_queue, "ping", broken)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["queue"] == "error: ConnectionError"


def test_every_response_names_its_instance(client, auth_headers) -> None:
    for path in ("/health", "/ready", "/v1/uploads/no-such-upload"):
        response = client.get(path, headers=auth_headers)
        assert response.headers["X-DFS-Instance"] == INSTANCE_ID


def test_log_lines_carry_the_instance_id(client, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="dfs.request"):
        client.get("/health")
    events = [json.loads(r.message) for r in caplog.records if r.name == "dfs.request"]
    assert events and all(event["instance_id"] == INSTANCE_ID for event in events)
