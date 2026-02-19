import json

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Chunk, ChunkRequestIdempotency, CompleteRequestIdempotency, InitRequestIdempotency, Upload


def _reset_state() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.execute(delete(ChunkRequestIdempotency))
        db.execute(delete(CompleteRequestIdempotency))
        db.execute(delete(InitRequestIdempotency))
        db.execute(delete(Chunk))
        db.execute(delete(Upload))
        db.commit()


def _events_from_caplog(caplog) -> list[dict]:
    events: list[dict] = []
    for record in caplog.records:
        if record.name != "dfs.audit":
            continue
        try:
            events.append(json.loads(record.message))
        except json.JSONDecodeError:
            continue
    return events


def test_audit_logs_for_init_complete_download(caplog) -> None:
    _reset_state()
    caplog.set_level("INFO", logger="dfs.audit")
    with TestClient(app) as client:
        init = client.post(
            "/v1/uploads/init",
            json={"file_name": "audit.bin", "file_size": 4, "chunk_size": 4},
            headers={"X-API-Key": "dev-key", "X-Request-ID": "req-audit"},
        )
        assert init.status_code == 201
        upload_id = init.json()["upload_id"]

        chunk = client.put(
            f"/v1/uploads/{upload_id}/chunks/0",
            content=b"abcd",
            headers={"X-API-Key": "dev-key", "Content-Length": "4", "X-Request-ID": "req-audit"},
        )
        assert chunk.status_code == 202

        complete = client.post(
            f"/v1/uploads/{upload_id}/complete",
            headers={"X-API-Key": "dev-key", "X-Request-ID": "req-audit"},
        )
        assert complete.status_code == 200

        download = client.get(
            f"/v1/uploads/{upload_id}/download",
            headers={"X-API-Key": "dev-key", "X-Request-ID": "req-audit"},
        )
        assert download.status_code == 200

    events = _events_from_caplog(caplog)
    actions = [event.get("action") for event in events]
    assert "upload_init" in actions
    assert "upload_complete" in actions
    assert "download" in actions
    init_event = next(event for event in events if event.get("action") == "upload_init")
    assert init_event["request_id"] == "req-audit"
    assert init_event["user_id"] == "dev-user"
    assert "trace_id" in init_event
