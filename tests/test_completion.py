"""Upload completion and abort: claims, leases, idempotent retries, and no DB connection held
during storage work."""

import hashlib
import json
import logging
import threading
import time
from datetime import UTC, datetime

import httpx
import pytest
from db_hooks import before_statement
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.models import Upload
from app.storage import storage


def _new_upload(client, headers, file_size: int, chunk_size: int, **extra) -> str:
    payload = {"file_name": "f.bin", "file_size": file_size, "chunk_size": chunk_size, **extra}
    response = client.post("/v1/uploads/init", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["upload_id"]


def _upload_chunks(client, headers, upload_id: str, data: bytes, chunk_size: int) -> None:
    for index in range(0, len(data), chunk_size):
        piece = data[index : index + chunk_size]
        response = client.put(f"/v1/uploads/{upload_id}/chunks/{index // chunk_size}", content=piece, headers=headers)
        assert response.status_code == 202, response.text


def _status(upload_id: str) -> str:
    with SessionLocal() as db:
        return db.get(Upload, upload_id).status


def _as_multipart(upload_id: str, monkeypatch) -> None:
    """Make a local-storage upload take the S3 multipart completion path, with fake etags."""
    from app.models import Chunk

    with SessionLocal() as db:
        db.execute(update(Upload).where(Upload.id == upload_id).values(multipart_upload_id="mp-test"))
        db.execute(update(Chunk).where(Chunk.upload_id == upload_id).values(s3_etag='"etag"'))
        db.commit()
    monkeypatch.setattr(settings, "storage_backend", "s3")


def _ready_multipart_upload(client, headers, monkeypatch) -> str:
    upload_id = _new_upload(client, headers, 10, 5)
    _upload_chunks(client, headers, upload_id, b"aaaaabbbbb", 5)
    _as_multipart(upload_id, monkeypatch)
    return upload_id


# ---------------------------------------------------------------- concurrency and leases


def test_concurrent_complete_calls_storage_once(client, auth_headers, monkeypatch) -> None:
    upload_id = _ready_multipart_upload(client, auth_headers, monkeypatch)
    calls: list[dict] = []

    def slow_complete(**kwargs) -> None:
        calls.append(kwargs)
        time.sleep(0.3)

    monkeypatch.setattr(storage, "complete_upload", slow_complete)
    statuses: list[int] = []

    def complete() -> None:
        statuses.append(client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code)

    threads = [threading.Thread(target=complete) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1, f"storage complete_upload called {len(calls)} times"
    assert sorted(statuses) == [200, 409]
    assert _status(upload_id) == "COMPLETED"


def test_complete_within_lease_returns_409(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 10, 5)
    _upload_chunks(client, auth_headers, upload_id, b"aaaaabbbbb", 5)
    with SessionLocal() as db:  # another request is completing it right now
        db.execute(
            update(Upload).where(Upload.id == upload_id).values(status="COMPLETING", updated_at=datetime.now(UTC))
        )
        db.commit()
    response = client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "completion in progress"
    assert _status(upload_id) == "COMPLETING"


def test_complete_retry_after_commit_crash_succeeds(client, auth_headers, monkeypatch) -> None:
    """S3 assembled the file, then the process died before recording COMPLETED. A retry after the
    lease expires must succeed even though the multipart upload no longer exists."""
    upload_id = _ready_multipart_upload(client, auth_headers, monkeypatch)
    assembled = {"done": False}

    def complete_once(**kwargs) -> None:
        if assembled["done"]:
            raise RuntimeError("InvalidPart: the multipart upload was already completed")
        assembled["done"] = True

    monkeypatch.setattr(storage, "complete_upload", complete_once)
    monkeypatch.setattr(storage, "object_exists", lambda key: assembled["done"] and key.endswith("/assembled"))

    def crash(conn, cursor, parameters) -> None:
        raise RuntimeError("simulated crash before COMPLETED was committed")

    with (
        before_statement("UPDATE uploads", crash, match=lambda params: "COMPLETED" in str(params)) as hook,
        TestClient(app, raise_server_exceptions=False) as tolerant,
    ):
        first = tolerant.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert hook.fired
    assert first.status_code == 500
    assert _status(upload_id) == "COMPLETING"

    assert client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code == 409  # lease held
    monkeypatch.setattr(settings, "completion_lease_seconds", 0, raising=False)
    retry = client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "COMPLETED"
    assert _status(upload_id) == "COMPLETED"


def test_failed_storage_completion_releases_the_claim(client, auth_headers, monkeypatch) -> None:
    upload_id = _ready_multipart_upload(client, auth_headers, monkeypatch)

    def failing_complete(**kwargs) -> None:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(storage, "complete_upload", failing_complete)
    monkeypatch.setattr(storage, "object_exists", lambda key: False)
    response = client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert response.status_code == 500
    assert _status(upload_id) == "IN_PROGRESS"  # the client can retry immediately


def test_empty_file_round_trip(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 0, 5)
    complete = client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] == "COMPLETED"
    download = client.get(f"/v1/uploads/{upload_id}/download", headers=auth_headers)
    assert download.status_code == 200
    assert download.content == b""


def test_complete_with_chunks_from_initiated_is_still_rejected(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 10, 5)
    response = client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert response.status_code == 409


# ---------------------------------------------------------------- DB connections during storage work


def test_complete_releases_db_connection_during_storage_work(client, auth_headers, monkeypatch) -> None:
    upload_id = _ready_multipart_upload(client, auth_headers, monkeypatch)
    held: list[int] = []

    def observed_complete(**kwargs) -> None:
        held.append(engine.pool.checkedout())

    monkeypatch.setattr(storage, "complete_upload", observed_complete)
    assert client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code == 200
    assert held == [0]


def test_concurrent_completes_with_slow_storage_exceeding_db_pool_succeed(
    live_server, small_pool, auth_headers, monkeypatch
) -> None:
    """Regression for the intermittent W2 failures: many completes whose storage work (the S3
    multipart completion) is slow must not hold DB connections while waiting on it."""
    uploads = 10
    data = b"aaaaabbbbb"

    def slow_complete(**kwargs) -> None:
        time.sleep(0.5)

    monkeypatch.setattr(storage, "complete_upload", slow_complete)
    with httpx.Client(base_url=live_server, timeout=30.0, limits=httpx.Limits(max_connections=uploads + 5)) as http:
        ids = [_new_upload(http, auth_headers, 10, 5) for _ in range(uploads)]
        for upload_id in ids:
            _upload_chunks(http, auth_headers, upload_id, data, 5)
            _as_multipart(upload_id, monkeypatch)
        statuses: list[int] = []

        def complete(upload_id: str) -> None:
            statuses.append(http.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code)

        threads = [threading.Thread(target=complete, args=(upload_id,)) for upload_id in ids]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.perf_counter() - started

    assert sorted(statuses) == [200] * uploads
    assert elapsed < 3.0, f"{uploads} completes took {elapsed:.1f} s (held DB connections during storage work)"


# ---------------------------------------------------------------- abort


def test_abort_marks_aborted_and_aborts_multipart(client, auth_headers, monkeypatch, caplog) -> None:
    upload_id = _new_upload(client, auth_headers, 10, 5)
    _upload_chunks(client, auth_headers, upload_id, b"aaaaa", 5)
    with SessionLocal() as db:
        db.execute(update(Upload).where(Upload.id == upload_id).values(multipart_upload_id="mp-test"))
        db.commit()
    aborted: list[tuple] = []
    monkeypatch.setattr(storage, "abort_upload", lambda uid, mp: aborted.append((uid, mp)))

    with caplog.at_level(logging.INFO, logger="dfs.audit"):
        response = client.delete(f"/v1/uploads/{upload_id}", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"upload_id": upload_id, "status": "ABORTED", "failure_reason": "aborted by client"}
    assert aborted == [(upload_id, "mp-test")]
    assert storage.list_keys(f"uploads/{upload_id}/") == []
    audit = [json.loads(r.message) for r in caplog.records if r.name == "dfs.audit"]
    assert any(e["action"] == "upload_abort" and e["upload_id"] == upload_id for e in audit)

    put = client.put(f"/v1/uploads/{upload_id}/chunks/1", content=b"bbbbb", headers=auth_headers)
    assert put.status_code == 409
    assert client.delete(f"/v1/uploads/{upload_id}", headers=auth_headers).status_code == 200  # idempotent
    assert client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code == 409


def test_abort_completed_upload_is_409(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 5, 5)
    _upload_chunks(client, auth_headers, upload_id, b"aaaaa", 5)
    assert client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code == 200
    response = client.delete(f"/v1/uploads/{upload_id}", headers=auth_headers)
    assert response.status_code == 409
    assert _status(upload_id) == "COMPLETED"


def test_abort_requires_ownership(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 5, 5)
    response = client.delete(f"/v1/uploads/{upload_id}", headers={"X-API-Key": "other-key"})
    assert response.status_code == 404  # indistinguishable from a missing upload
    assert _status(upload_id) == "INITIATED"


def test_cleanup_and_storage_share_the_assembled_key() -> None:
    assert storage.assembled_key("u1") == "uploads/u1/assembled"


def test_storage_object_exists_local() -> None:
    key = storage.write_chunk("exists-check", 0, b"x").key
    assert storage.object_exists(key) is True
    assert storage.object_exists("uploads/missing/chunk_0") is False


# ---------------------------------------------------------------- multipart: one write per chunk


def _b64_sha256(data: bytes) -> str:
    import base64

    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def test_complete_sends_part_checksums(client, auth_headers, monkeypatch) -> None:
    upload_id = _ready_multipart_upload(client, auth_headers, monkeypatch)
    seen: list[list[dict]] = []
    monkeypatch.setattr(settings, "s3_part_checksums", True, raising=False)
    monkeypatch.setattr(storage, "complete_upload", lambda **kwargs: seen.append(kwargs["parts"]))

    assert client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code == 200
    assert seen == [
        [
            {"PartNumber": 1, "ETag": '"etag"', "ChecksumSHA256": _b64_sha256(b"aaaaa")},
            {"PartNumber": 2, "ETag": '"etag"', "ChecksumSHA256": _b64_sha256(b"bbbbb")},
        ]
    ]


def test_multipart_download_streams_the_assembled_object(client, auth_headers, monkeypatch) -> None:
    upload_id = _ready_multipart_upload(client, auth_headers, monkeypatch)
    monkeypatch.setattr(storage, "complete_upload", lambda **kwargs: None)
    assert client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code == 200
    read_keys: list[str] = []

    def stream_assembled(key: str, start: int, end: int, block_size: int = 8 * 1024 * 1024):
        read_keys.append(key)
        yield b"aaaaabbbbb"[start : end + 1]

    monkeypatch.setattr(settings, "download_prefetch", 1)  # one streaming read per piece
    monkeypatch.setattr(storage, "iter_range", stream_assembled)
    monkeypatch.setattr(storage, "read_chunk", lambda key: pytest.fail(f"read a per-chunk object {key}"))

    full = client.get(f"/v1/uploads/{upload_id}/download", headers=auth_headers)
    assert full.status_code == 200
    assert full.content == b"aaaaabbbbb"
    partial = client.get(f"/v1/uploads/{upload_id}/download", headers={**auth_headers, "Range": "bytes=3-6"})
    assert partial.status_code == 206
    assert partial.content == b"aabb"
    assert read_keys == [f"uploads/{upload_id}/assembled"] * 2
