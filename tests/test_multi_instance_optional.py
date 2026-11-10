"""Two app instances behind nginx (compose profile "cluster"), plus a separate verification worker.

Start it with:  docker compose --profile cluster up -d --build nginx finalizer-cluster
Enable with RUN_MULTI_INSTANCE=1; MULTI_INSTANCE_BASE_URL defaults to http://localhost:8080.
"""

import hashlib
import os
import threading
import time

import httpx
import pytest

BASE_URL = os.getenv("MULTI_INSTANCE_BASE_URL", "http://localhost:8080")
UPLOADER = {"X-API-Key": "dev-key"}
LIMITED = {"X-API-Key": "limit-key"}  # a user nothing else in this file uses
RATE_LIMIT = 100  # API_RATE_LIMIT_PER_MINUTE in the cluster profile
MIB = 1024 * 1024

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MULTI_INSTANCE") != "1", reason="Set RUN_MULTI_INSTANCE=1 (see module docstring)."
)


@pytest.fixture
def http():
    with httpx.Client(base_url=BASE_URL, timeout=60.0, limits=httpx.Limits(max_connections=200)) as client:
        yield client


def _request(http, method: str, url: str, **kwargs) -> httpx.Response:
    """Send, honoring Retry-After on 429 so the lifecycle test never trips its own rate limit."""
    for _ in range(10):
        response = http.request(method, url, **kwargs)
        if response.status_code != 429:
            return response
        time.sleep(float(response.headers.get("Retry-After", "1")))
    return response


def test_both_instances_are_ready(http) -> None:
    seen = set()
    for _ in range(20):
        response = http.get("/ready")
        assert response.status_code == 200, response.text
        seen.add(response.json()["instance_id"])
    assert len(seen) >= 2, seen


def test_upload_lifecycle_across_instances(http) -> None:
    data = os.urandom(4 * 5 * MIB + MIB)  # four 5 MiB parts and a 1 MiB last part
    init = _request(
        http,
        "POST",
        "/v1/uploads/init",
        json={
            "file_name": "cluster.bin",
            "file_size": len(data),
            "chunk_size": 5 * MIB,
            "file_checksum_sha256": hashlib.sha256(data).hexdigest(),
        },
        headers=UPLOADER,
    )
    assert init.status_code == 201, init.text
    upload_id = init.json()["upload_id"]

    served_by = set()
    for index in range(0, len(data), 5 * MIB):
        response = _request(
            http,
            "PUT",
            f"/v1/uploads/{upload_id}/chunks/{index // (5 * MIB)}",
            content=data[index : index + 5 * MIB],
            headers=UPLOADER,
        )
        assert response.status_code == 202, response.text
        served_by.add(response.headers["X-DFS-Instance"])
    assert len(served_by) >= 2, f"all chunks went to one instance: {served_by}"

    complete = _request(http, "POST", f"/v1/uploads/{upload_id}/complete", headers=UPLOADER)
    assert complete.status_code == 202, complete.text  # verified by finalizer-cluster, a third process
    deadline = time.monotonic() + 60
    while (
        status := _request(http, "GET", f"/v1/uploads/{upload_id}", headers=UPLOADER).json()["status"]
    ) == "VERIFYING":
        assert time.monotonic() < deadline, "verification did not finish"
        time.sleep(1)
    assert status == "COMPLETED"

    download = _request(http, "GET", f"/v1/uploads/{upload_id}/download", headers=UPLOADER)
    assert hashlib.sha256(download.content).hexdigest() == hashlib.sha256(data).hexdigest()
    ranged = _request(
        http, "GET", f"/v1/uploads/{upload_id}/download", headers={**UPLOADER, "Range": "bytes=5242870-5242890"}
    )
    assert ranged.status_code == 206
    assert ranged.content == data[5242870:5242891]


def test_rate_limit_is_shared_across_instances(http) -> None:
    # The window is per minute; start with at least 15 s left so all requests share one window.
    while 60 - time.time() % 60 < 15:
        time.sleep(1)
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def call() -> None:
        response = http.get("/v1/uploads/no-such-upload", headers=LIMITED)
        with lock:
            results.append((response.status_code, response.headers.get("X-DFS-Instance", "?")))

    threads = [threading.Thread(target=call) for _ in range(RATE_LIMIT + 30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    allowed = [instance for status, instance in results if status == 404]
    throttled = [status for status, _ in results if status == 429]
    assert len(allowed) == RATE_LIMIT, (
        f"{len(allowed)} allowed; a per-instance limit would allow up to {2 * RATE_LIMIT}"
    )
    assert len(throttled) == 30
    assert len(set(allowed)) >= 2  # the shared budget was spent on both instances
