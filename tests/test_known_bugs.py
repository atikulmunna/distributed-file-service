"""Bugs found in the robustness review, pinned as strict expected failures.

Each test asserts the correct behavior and is marked xfail(strict=True), so it must fail
today. When the task that fixes it lands, the test starts passing, strict xfail turns that
into a suite failure, and the fixing task removes the marker and moves the test next to the
feature it covers. This file should be empty (and deleted) after Task 13.
"""

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.maintenance import cleanup_once
from app.models import Chunk, Upload
from app.storage import storage


def known_bug(task: int):
    return pytest.mark.xfail(strict=True, reason=f"known bug, fixed in Task {task}")


def _init(client: TestClient, headers: dict, file_size: int, chunk_size: int, file_name: str = "f.bin") -> str:
    response = client.post(
        "/v1/uploads/init",
        json={"file_name": file_name, "file_size": file_size, "chunk_size": chunk_size},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["upload_id"]


def _put(client: TestClient, headers: dict, upload_id: str, index: int, body: bytes):
    return client.put(f"/v1/uploads/{upload_id}/chunks/{index}", content=body, headers=headers)


def _completed_upload(client: TestClient, headers: dict, body: bytes, file_name: str = "f.bin") -> str:
    upload_id = _init(client, headers, len(body), len(body), file_name)
    assert _put(client, headers, upload_id, 0, body).status_code == 202
    assert client.post(f"/v1/uploads/{upload_id}/complete", headers=headers).status_code == 200
    return upload_id


def _start_parallel_puts(client: TestClient, headers: dict, upload_id: str, count: int, statuses: list[int]):
    def put(index: int) -> None:
        statuses.append(_put(client, headers, upload_id, index, b"x" * 10).status_code)

    threads = [threading.Thread(target=put, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    return threads


@known_bug(4)
def test_parallel_chunk_puts_overlap(client, auth_headers, slow_storage) -> None:
    slow_storage(0.5)
    upload_id = _init(client, auth_headers, 40, 10)
    statuses: list[int] = []
    started = time.perf_counter()
    for thread in _start_parallel_puts(client, auth_headers, upload_id, 4, statuses):
        thread.join()
    elapsed = time.perf_counter() - started
    assert sorted(statuses) == [202] * 4
    assert elapsed < 1.2, f"4 chunk PUTs with 0.5 s writes took {elapsed:.2f} s (serialized)"


@known_bug(4)
def test_health_responsive_during_uploads(client, auth_headers, slow_storage) -> None:
    slow_storage(0.5)
    upload_id = _init(client, auth_headers, 40, 10)
    statuses: list[int] = []
    threads = _start_parallel_puts(client, auth_headers, upload_id, 4, statuses)
    time.sleep(0.1)
    started = time.perf_counter()
    health = client.get("/health")
    latency = time.perf_counter() - started
    for thread in threads:
        thread.join()
    assert health.status_code == 200
    assert latency < 0.2, f"/health took {latency:.2f} s while chunk uploads ran"


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="32-bit INTEGER overflow only exists on Postgres")
@known_bug(5)
def test_file_over_2gib_persists(client, auth_headers) -> None:
    response = client.post(
        "/v1/uploads/init",
        json={"file_name": "big.bin", "file_size": 3 * 1024**3},
        headers=auth_headers,
    )
    assert response.status_code == 201


@known_bug(6)
def test_chunk_size_must_match_layout(client, auth_headers) -> None:
    upload_id = _init(client, auth_headers, 10, 5)
    assert _put(client, auth_headers, upload_id, 0, b"A" * 100).status_code == 400


@known_bug(6)
def test_init_rejects_too_many_chunks(client, auth_headers) -> None:
    response = client.post(
        "/v1/uploads/init",
        json={"file_name": "tiny-chunks.bin", "file_size": 10**9, "chunk_size": 1},
        headers=auth_headers,
    )
    assert response.status_code in (400, 422)


@known_bug(7)
def test_concurrent_same_chunk_puts_succeed(client, auth_headers) -> None:
    """Another request commits the same chunk between our existence check and our insert.

    Simulated deterministically: right before the server's INSERT into chunks, a row for the
    same (upload_id, chunk_index) is written in the same transaction, as a racing writer would.
    """
    upload_id = _init(client, auth_headers, 10, 5)
    body = b"abcde"
    fired: list[bool] = []

    def inject_racing_row(conn, cursor, statement, parameters, context, executemany):
        if fired or not statement.lstrip().upper().startswith("INSERT INTO CHUNKS "):
            return
        fired.append(True)
        now = datetime.now(timezone.utc)
        if conn.dialect.name == "sqlite":
            marks, stamp = ", ".join(["?"] * 8), now.strftime("%Y-%m-%d %H:%M:%S.%f")
        else:
            marks, stamp = ", ".join(["%s"] * 8), now
        racing = cursor.connection.cursor()
        racing.execute(
            "INSERT INTO chunks (upload_id, chunk_index, size_bytes, s3_key, status, retry_count, created_at, updated_at)"
            f" VALUES ({marks})",
            (upload_id, 0, 5, f"uploads/{upload_id}/chunk_0", "UPLOADED", 0, stamp, stamp),
        )
        racing.close()

    event.listen(engine, "before_cursor_execute", inject_racing_row)
    try:
        response = _put(client, auth_headers, upload_id, 0, body)
    finally:
        event.remove(engine, "before_cursor_execute", inject_racing_row)

    assert fired, "the racing insert never ran"
    assert response.status_code == 202
    with SessionLocal() as db:
        rows = list(db.scalars(select(Chunk).where(Chunk.upload_id == upload_id, Chunk.chunk_index == 0)))
    assert len(rows) == 1
    assert rows[0].chunk_checksum_sha256 == hashlib.sha256(body).hexdigest()


@known_bug(8)
def test_concurrent_complete_calls_storage_once(client, auth_headers, monkeypatch) -> None:
    upload_id = _init(client, auth_headers, 10, 5)
    assert _put(client, auth_headers, upload_id, 0, b"aaaaa").status_code == 202
    assert _put(client, auth_headers, upload_id, 1, b"bbbbb").status_code == 202
    with SessionLocal() as db:
        db.execute(update(Upload).where(Upload.id == upload_id).values(multipart_upload_id="mp-test"))
        db.execute(update(Chunk).where(Chunk.upload_id == upload_id).values(s3_etag='"etag"'))
        db.commit()

    calls: list[dict] = []

    def slow_complete(**kwargs) -> None:
        calls.append(kwargs)
        time.sleep(0.3)

    monkeypatch.setattr(storage, "complete_upload", slow_complete)
    monkeypatch.setattr(settings, "storage_backend", "s3")
    statuses: list[int] = []

    def complete() -> None:
        statuses.append(client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code)

    threads = [threading.Thread(target=complete) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1, f"storage complete_upload called {len(calls)} times"
    with SessionLocal() as db:
        assert db.get(Upload, upload_id).status == "COMPLETED"


@known_bug(11)
def test_range_suffix_returns_tail(client, auth_headers) -> None:
    upload_id = _completed_upload(client, auth_headers, b"0123456789")
    response = client.get(f"/v1/uploads/{upload_id}/download", headers={**auth_headers, "Range": "bytes=-3"})
    assert response.status_code == 206
    assert response.content == b"789"


@known_bug(11)
def test_malformed_range_does_not_500(client, auth_headers) -> None:
    upload_id = _completed_upload(client, auth_headers, b"0123456789")
    response = client.get(f"/v1/uploads/{upload_id}/download", headers={**auth_headers, "Range": "bytes=abc-"})
    assert response.status_code == 200
    assert response.content == b"0123456789"


@known_bug(11)
def test_non_ascii_filename_download(client, auth_headers) -> None:
    name = "রিপোর্ট.txt"  # Bengali "report"
    upload_id = _completed_upload(client, auth_headers, b"data", file_name=name)
    response = client.get(f"/v1/uploads/{upload_id}/download", headers=auth_headers)
    assert response.status_code == 200
    assert response.content == b"data"


@known_bug(12)
def test_durable_queue_chunk_put(auth_headers, monkeypatch) -> None:
    # The queue consumer and the waiting request share one event loop; the request blocks it.
    monkeypatch.setattr(settings, "queue_backend", "redis")
    monkeypatch.setattr(settings, "queue_task_timeout_seconds", 2)
    monkeypatch.setattr(settings, "queue_poll_timeout_seconds", 1)
    with TestClient(app) as client:
        upload_id = _init(client, auth_headers, 10, 10)
        assert _put(client, auth_headers, upload_id, 0, b"y" * 10).status_code == 202


@known_bug(13)
def test_old_but_active_upload_survives_cleanup(client, auth_headers) -> None:
    upload_id = _init(client, auth_headers, 10, 5)
    assert _put(client, auth_headers, upload_id, 0, b"aaaaa").status_code == 202
    two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
    with SessionLocal() as db:
        db.execute(update(Upload).where(Upload.id == upload_id).values(created_at=two_days_ago))
        db.commit()
    with SessionLocal() as db:
        cleanup_once(db)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Upload).where(Upload.id == upload_id)) == 1


@known_bug(13)
def test_recent_orphan_object_is_kept() -> None:
    key = storage.write_chunk("orphan-upload", 0, b"x").key
    with SessionLocal() as db:
        cleanup_once(db)
    assert key in storage.list_keys("uploads/")
