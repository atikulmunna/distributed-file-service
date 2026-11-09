"""Concurrency behavior of the chunk upload path.

The chunk handler must never block the event loop and must never hold a DB connection
while it waits on the network (request body) or on object storage.
"""

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app


def _init(client, headers: dict, file_size: int, chunk_size: int) -> str:
    response = client.post(
        "/v1/uploads/init",
        json={"file_name": "f.bin", "file_size": file_size, "chunk_size": chunk_size},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["upload_id"]


def test_no_db_connection_held_during_storage_write(client, auth_headers, slow_storage) -> None:
    slow_storage(0.5)
    upload_id = _init(client, auth_headers, 10, 10)
    statuses: list[int] = []
    put = threading.Thread(
        target=lambda: statuses.append(
            client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"x" * 10, headers=auth_headers).status_code
        )
    )
    put.start()
    time.sleep(0.25)  # the storage write is sleeping now
    held_during_write = engine.pool.checkedout()
    put.join()
    assert statuses == [202]
    assert held_during_write == 0


@pytest.fixture
def small_pool():
    """Bind sessions to a 2-connection pool with a 3 s checkout timeout for one test."""
    small = create_engine(settings.database_url, pool_size=2, max_overflow=0, pool_timeout=3)
    SessionLocal.configure(bind=small)
    try:
        yield small
    finally:
        SessionLocal.configure(bind=engine)
        small.dispose()


@pytest.fixture
def live_server():
    """Run the app on a real uvicorn server (TestClient hands bodies over without yielding)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_concurrent_slow_uploads_exceeding_db_pool_do_not_deadlock(live_server, small_pool, auth_headers) -> None:
    """Regression for the W1 baseline lockup: more slow-body chunk uploads than DB connections."""
    uploads = 6  # three times the pool size

    def slow_body():
        yield b"x" * 5
        time.sleep(0.5)
        yield b"y" * 5

    with httpx.Client(base_url=live_server, timeout=30.0, limits=httpx.Limits(max_connections=20)) as http:
        upload_id = _init(http, auth_headers, 10 * uploads, 10)
        statuses: list[int] = []

        def put(index: int) -> None:
            response = http.put(
                f"/v1/uploads/{upload_id}/chunks/{index}",
                content=slow_body(),
                headers={**auth_headers, "Content-Length": "10"},
            )
            statuses.append(response.status_code)

        threads = [threading.Thread(target=put, args=(i,)) for i in range(uploads)]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.perf_counter() - started

        assert sorted(statuses) == [202] * uploads
        assert elapsed < 3.0, f"{uploads} slow uploads took {elapsed:.1f} s (waited on the DB pool)"
        assert http.get("/health", timeout=2.0).status_code == 200


def _start_parallel_puts(client: TestClient, headers: dict, upload_id: str, count: int, statuses: list[int]):
    def put(index: int) -> None:
        statuses.append(client.put(f"/v1/uploads/{upload_id}/chunks/{index}", content=b"x" * 10, headers=headers).status_code)

    threads = [threading.Thread(target=put, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    return threads


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


def test_no_db_connection_held_during_storage_init(client, auth_headers, monkeypatch) -> None:
    from app.storage import storage

    def slow_initialize_upload(upload_id: str) -> str:
        time.sleep(0.5)
        return "mp-test"

    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(storage, "initialize_upload", slow_initialize_upload)
    responses: list = []
    init = threading.Thread(
        target=lambda: responses.append(
            client.post(
                "/v1/uploads/init",
                # 3 chunks of 5 MiB minimum part size, so the S3 multipart upload is created at init
                json={"file_name": "mp.bin", "file_size": 12 * 1024 * 1024, "chunk_size": 5 * 1024 * 1024},
                headers=auth_headers,
            )
        )
    )
    init.start()
    time.sleep(0.25)  # CreateMultipartUpload is sleeping now
    held_during_init = engine.pool.checkedout()
    init.join()

    assert responses[0].status_code == 201, responses[0].text
    upload_id = responses[0].json()["upload_id"]
    with SessionLocal() as db:
        from app.models import Upload

        assert db.get(Upload, upload_id).multipart_upload_id == "mp-test"
    assert held_during_init == 0
