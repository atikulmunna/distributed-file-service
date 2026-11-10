"""Cleanup: stale by last activity, multipart uploads aborted, orphans only after a grace period, one
instance at a time, every error logged and counted, and never a VERIFYING or COMPLETED upload."""

import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import create_engine, func, select, text, update

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.maintenance import CLEANUP_LOCK_KEY, cleanup_once
from app.models import Chunk, Upload
from app.storage import storage

OLD = datetime.now(UTC) - timedelta(days=2)


def _upload(client, headers, chunks: int = 1) -> str:
    response = client.post(
        "/v1/uploads/init", json={"file_name": "f.bin", "file_size": 5 * chunks, "chunk_size": 5}, headers=headers
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["upload_id"]
    for index in range(chunks):
        assert (
            client.put(f"/v1/uploads/{upload_id}/chunks/{index}", content=b"aaaaa", headers=headers).status_code == 202
        )
    return upload_id


def _set(upload_id: str, **values) -> None:
    with SessionLocal() as db:
        db.execute(update(Upload).where(Upload.id == upload_id).values(**values))
        db.commit()


def _exists(upload_id: str) -> bool:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Upload).where(Upload.id == upload_id)) == 1


def _age_file(key: str, seconds: float) -> None:
    path = os.path.join(settings.storage_root, key)
    past = time.time() - seconds
    os.utime(path, (past, past))


def _errors(step: str) -> float:
    return REGISTRY.get_sample_value("cleanup_errors_total", {"step": step}) or 0.0


# ---------------------------------------------------------------- what counts as stale


def test_old_but_active_upload_survives_cleanup(client, auth_headers) -> None:
    upload_id = _upload(client, auth_headers)
    _set(upload_id, created_at=OLD)  # started long ago, but a chunk arrived just now
    cleanup_once()
    assert _exists(upload_id)


def test_inactive_upload_is_cleaned_with_its_objects(client, auth_headers) -> None:
    upload_id = _upload(client, auth_headers, chunks=2)
    _set(upload_id, last_activity_at=OLD)
    stats = cleanup_once()
    assert stats["stale_uploads_deleted"] == 1
    assert not _exists(upload_id)
    assert storage.list_keys(f"uploads/{upload_id}/") == []
    with SessionLocal() as db:  # chunk rows go with the upload (FK cascade, also on SQLite)
        assert db.scalar(select(func.count()).select_from(Chunk).where(Chunk.upload_id == upload_id)) == 0


def test_last_activity_moves_with_chunks_and_transitions(client, auth_headers) -> None:
    upload_id = _upload(client, auth_headers, chunks=1)
    _set(upload_id, last_activity_at=OLD)
    assert client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"aaaaa", headers=auth_headers).status_code == 202
    with SessionLocal() as db:
        after_chunk = db.get(Upload, upload_id).last_activity_at
    assert after_chunk.replace(tzinfo=UTC) > OLD + timedelta(days=1)
    _set(upload_id, last_activity_at=OLD)
    assert client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code == 200
    with SessionLocal() as db:
        assert db.get(Upload, upload_id).last_activity_at.replace(tzinfo=UTC) > OLD + timedelta(days=1)


@pytest.mark.parametrize("status", ["FAILED", "ABORTED", "COMPLETING", "INITIATED"])
def test_failed_aborted_and_abandoned_uploads_cleaned(client, auth_headers, status) -> None:
    upload_id = _upload(client, auth_headers)
    _set(upload_id, status=status, last_activity_at=OLD)
    cleanup_once()
    assert not _exists(upload_id)


def test_completed_and_verifying_uploads_are_never_cleaned(client, auth_headers) -> None:
    completed = _upload(client, auth_headers)
    verifying = _upload(client, auth_headers)
    _set(completed, status="COMPLETED", last_activity_at=OLD)
    _set(verifying, status="VERIFYING", last_activity_at=OLD)
    stats = cleanup_once()
    assert _exists(completed)
    assert _exists(verifying)
    assert stats["uploads_stuck_verifying"] == 1  # reported for alerting, never deleted


def test_stale_upload_aborts_multipart(client, auth_headers, monkeypatch) -> None:
    upload_id = _upload(client, auth_headers)
    _set(upload_id, multipart_upload_id="mp-test", last_activity_at=OLD)
    aborted: list[tuple[str, str]] = []
    monkeypatch.setattr(storage, "abort_upload", lambda uid, mp: aborted.append((uid, mp)))
    stats = cleanup_once()
    assert aborted == [(upload_id, "mp-test")]
    assert stats["multipart_uploads_aborted"] == 1
    assert not _exists(upload_id)


def test_failed_abort_keeps_the_upload_for_the_next_run(client, auth_headers, monkeypatch, caplog) -> None:
    upload_id = _upload(client, auth_headers)
    _set(upload_id, multipart_upload_id="mp-test", last_activity_at=OLD)

    def failing_abort(uid, mp):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(storage, "abort_upload", failing_abort)
    before = _errors("abort_multipart")
    with caplog.at_level(logging.INFO, logger="dfs.request"):
        cleanup_once()
    assert _exists(upload_id)  # deleting the row now would lose the only reference to the parts
    assert _errors("abort_multipart") == before + 1
    events = [json.loads(r.message) for r in caplog.records if r.name == "dfs.request"]
    assert any(e.get("event") == "cleanup_error" and e.get("upload_id") == upload_id for e in events)


# ---------------------------------------------------------------- orphan objects


def test_recent_orphan_object_is_kept() -> None:
    key = storage.write_chunk("orphan-upload", 0, b"x").key  # written, but its DB row is not committed yet
    cleanup_once()
    assert key in storage.list_keys("uploads/")


def test_old_orphan_is_deleted() -> None:
    key = storage.write_chunk("orphan-upload", 0, b"x").key
    _age_file(key, settings.orphan_grace_seconds + 60)
    stats = cleanup_once()
    assert key not in storage.list_keys("uploads/")
    assert stats["orphans_deleted"] == 1


def test_referenced_old_objects_are_kept(client, auth_headers) -> None:
    upload_id = _upload(client, auth_headers)
    key = f"uploads/{upload_id}/chunk_0"
    _age_file(key, settings.orphan_grace_seconds + 60)
    cleanup_once()
    assert key in storage.list_keys("uploads/")


def test_cleanup_error_logged_and_counted(client, auth_headers, monkeypatch, caplog) -> None:
    upload_id = _upload(client, auth_headers)
    _set(upload_id, last_activity_at=OLD)

    def failing_delete(key: str) -> None:
        raise RuntimeError("delete refused")

    monkeypatch.setattr(storage, "delete_key", failing_delete)
    before = _errors("delete_object")
    with caplog.at_level(logging.INFO, logger="dfs.request"):
        stats = cleanup_once()
    assert stats["errors"] >= 1
    assert _errors("delete_object") > before
    assert not _exists(upload_id)  # its leftover objects are now orphans, removed by a later run
    events = [json.loads(r.message) for r in caplog.records if r.name == "dfs.request"]
    assert any(e.get("event") == "cleanup_error" and e.get("step") == "delete_object" for e in events)


# ---------------------------------------------------------------- one instance at a time, off the event loop


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="advisory locks are Postgres-only")
def test_cleanup_skips_when_lock_held(client, auth_headers) -> None:
    upload_id = _upload(client, auth_headers)
    _set(upload_id, last_activity_at=OLD)
    other = create_engine(settings.database_url)
    try:
        with other.connect() as holder:  # another instance is cleaning up
            assert holder.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": CLEANUP_LOCK_KEY})
            stats = cleanup_once()
            holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": CLEANUP_LOCK_KEY})
    finally:
        other.dispose()
    assert stats["skipped"] is True
    assert _exists(upload_id)
    assert cleanup_once()["skipped"] is False
    assert not _exists(upload_id)


def test_periodic_cleanup_does_not_block_the_event_loop(auth_headers, monkeypatch) -> None:
    started = threading.Event()

    def slow_cleanup() -> dict:
        started.set()
        time.sleep(1.0)
        return {}

    monkeypatch.setattr(settings, "cleanup_enabled", True)
    monkeypatch.setattr("app.main.cleanup_once", slow_cleanup)
    with TestClient(app) as client:
        assert started.wait(2)
        t0 = time.perf_counter()
        assert client.get("/health").status_code == 200
        assert time.perf_counter() - t0 < 0.2


# ---------------------------------------------------------------- admin endpoint


def test_admin_cleanup_endpoint_requires_api_key() -> None:
    with TestClient(app) as client:
        resp = client.post("/v1/admin/cleanup")
        assert resp.status_code == 401


def test_admin_cleanup_endpoint_requires_admin_role(monkeypatch) -> None:
    monkeypatch.setattr(settings, "api_key_mappings", "user-key:user-a,admin-key:user-admin")
    monkeypatch.setattr(settings, "admin_user_ids", "user-admin")
    with TestClient(app) as client:
        forbidden = client.post("/v1/admin/cleanup", headers={"X-API-Key": "user-key"})
        assert forbidden.status_code == 403
        allowed = client.post("/v1/admin/cleanup", headers={"X-API-Key": "admin-key"})
        assert allowed.status_code == 200
        assert allowed.json()["requested_by"] == "user-admin"
        assert allowed.json()["skipped"] is False
