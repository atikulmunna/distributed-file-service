"""Read APIs for the UI: limits, the upload list, upload detail, the chunk list, and live stats."""

import hashlib

from app.config import settings
from app.finalizer import process
from app.jobs import FinalizeJob
from app.logging_utils import INSTANCE_ID
from app.worker import executor

OTHER_USER = {"X-API-Key": "other-key"}


def _init(client, headers, file_size: int = 10, chunk_size: int = 4, checksum: str | None = None) -> str:
    payload = {"file_name": "f.bin", "file_size": file_size, "chunk_size": chunk_size}
    if checksum:
        payload["file_checksum_sha256"] = checksum
    response = client.post("/v1/uploads/init", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["upload_id"]


def _put(client, headers, upload_id: str, index: int, data: bytes) -> None:
    response = client.put(f"/v1/uploads/{upload_id}/chunks/{index}", content=data, headers=headers)
    assert response.status_code == 202, response.text


# ---- limits


def test_limits_match_settings(client, auth_headers, monkeypatch) -> None:
    body = client.get("/v1/limits", headers=auth_headers).json()
    assert body == {
        "max_file_size_bytes": settings.max_file_size_bytes,
        "min_chunk_size_bytes": settings.min_chunk_size_bytes,
        "max_chunk_size_bytes": settings.max_chunk_size_bytes,
        "default_chunk_size_bytes": settings.chunk_size_bytes,
        "max_total_chunks": settings.max_total_chunks,
        "max_inflight_chunks_per_upload": settings.max_inflight_chunks_per_upload,
        "storage_backend_requires_min_part": False,
        "min_part_size_bytes": None,
    }
    monkeypatch.setattr(settings, "storage_backend", "s3")
    s3 = client.get("/v1/limits", headers=auth_headers).json()
    assert s3["storage_backend_requires_min_part"] is True
    assert s3["min_part_size_bytes"] == 5 * 1024 * 1024


def test_limits_require_authentication(client) -> None:
    assert client.get("/v1/limits").status_code == 401


# ---- upload list


def test_list_uploads_owner_only_and_paginated(client, auth_headers) -> None:
    mine = {_init(client, auth_headers) for _ in range(5)}
    theirs = {_init(client, OTHER_USER) for _ in range(2)}

    everything = client.get("/v1/uploads?limit=200", headers=auth_headers).json()
    assert {item["upload_id"] for item in everything["items"]} == mine
    assert everything["next_cursor"] is None
    created = [item["created_at"] for item in everything["items"]]
    assert created == sorted(created, reverse=True)  # newest first

    pages, cursor = [], None
    while True:
        url = "/v1/uploads?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(url, headers=auth_headers).json()
        assert len(page["items"]) <= 2
        pages.extend(item["upload_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == [item["upload_id"] for item in everything["items"]]  # same order, no gaps or repeats

    assert {item["upload_id"] for item in client.get("/v1/uploads", headers=OTHER_USER).json()["items"]} == theirs


def test_list_uploads_summary_and_status_filter(client, auth_headers) -> None:
    upload_id = _init(client, auth_headers)
    _put(client, auth_headers, upload_id, 0, b"aaaa")
    empty = _init(client, auth_headers, file_size=0)
    assert client.post(f"/v1/uploads/{empty}/complete", headers=auth_headers).status_code == 200

    items = {item["upload_id"]: item for item in client.get("/v1/uploads", headers=auth_headers).json()["items"]}
    summary = items[upload_id]
    assert summary["file_name"] == "f.bin"
    assert summary["file_size"] == 10
    assert summary["total_chunks"] == 3
    assert summary["uploaded_chunks"] == 1
    assert summary["status"] == "IN_PROGRESS"

    completed = client.get("/v1/uploads?status=COMPLETED", headers=auth_headers).json()["items"]
    assert [item["upload_id"] for item in completed] == [empty]


def test_list_uploads_rejects_bad_input(client, auth_headers) -> None:
    assert client.get("/v1/uploads?status=DONE", headers=auth_headers).status_code == 422
    assert client.get("/v1/uploads?limit=0", headers=auth_headers).status_code == 422
    assert client.get("/v1/uploads?limit=201", headers=auth_headers).status_code == 422
    bad_cursor = client.get("/v1/uploads?cursor=not-a-cursor", headers=auth_headers)
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["detail"] == "invalid cursor"


# ---- upload detail


def test_upload_detail_counts_match_chunks(client, auth_headers) -> None:
    upload_id = _init(client, auth_headers, file_size=10, chunk_size=4)
    _put(client, auth_headers, upload_id, 0, b"aaaa")
    _put(client, auth_headers, upload_id, 2, b"cc")
    detail = client.get(f"/v1/uploads/{upload_id}", headers=auth_headers).json()
    assert detail["upload_id"] == upload_id
    assert detail["file_name"] == "f.bin"
    assert detail["file_size"] == 10
    assert detail["chunk_size"] == 4
    assert detail["total_chunks"] == 3
    assert detail["uploaded_chunks"] == 2
    assert detail["uploaded_bytes"] == 6
    assert detail["status"] == "IN_PROGRESS"
    assert detail["file_checksum_sha256"] is None
    assert detail["checksum_verified"] is None  # nothing to verify
    assert detail["failure_reason"] is None
    for field in ("created_at", "updated_at", "last_activity_at"):
        # Explicit UTC on every database (SQLite returns naive datetimes): a browser must not read local time.
        assert detail[field].endswith("Z"), detail[field]
    listed = client.get("/v1/uploads", headers=auth_headers).json()["items"][0]
    assert listed["created_at"].endswith("Z")
    chunk = client.get(f"/v1/uploads/{upload_id}/chunks", headers=auth_headers).json()["items"][0]
    assert chunk["updated_at"].endswith("Z")


def test_upload_detail_reports_the_verification_result(client, auth_headers) -> None:
    data = b"aaaabbbbcc"
    good = _init(client, auth_headers, checksum=hashlib.sha256(data).hexdigest())
    bad = _init(client, auth_headers, checksum="0" * 64)
    for upload_id in (good, bad):
        for index in range(3):
            _put(client, auth_headers, upload_id, index, data[index * 4 : index * 4 + 4])
        assert client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code == 202
        assert client.get(f"/v1/uploads/{upload_id}", headers=auth_headers).json()["checksum_verified"] is None
        process(FinalizeJob(upload_id))

    verified = client.get(f"/v1/uploads/{good}", headers=auth_headers).json()
    assert verified["status"] == "COMPLETED"
    assert verified["checksum_verified"] is True
    mismatch = client.get(f"/v1/uploads/{bad}", headers=auth_headers).json()
    assert mismatch["status"] == "FAILED"
    assert mismatch["checksum_verified"] is False


# ---- chunk list


def test_chunks_page_bounds(client, auth_headers) -> None:
    upload_id = _init(client, auth_headers, file_size=20, chunk_size=4)
    for index in range(5):
        _put(client, auth_headers, upload_id, index, bytes([65 + index]) * 4)

    first = client.get(f"/v1/uploads/{upload_id}/chunks?offset=0&limit=2", headers=auth_headers).json()
    assert first["total"] == 5
    assert [chunk["index"] for chunk in first["items"]] == [0, 1]
    chunk = first["items"][0]
    assert chunk["size_bytes"] == 4
    assert chunk["sha256"] == hashlib.sha256(b"AAAA").hexdigest()
    assert chunk["status"] == "UPLOADED"
    assert chunk["retry_count"] == 0
    assert chunk["updated_at"]

    last = client.get(f"/v1/uploads/{upload_id}/chunks?offset=4&limit=2", headers=auth_headers).json()
    assert [chunk["index"] for chunk in last["items"]] == [4]
    beyond = client.get(f"/v1/uploads/{upload_id}/chunks?offset=10", headers=auth_headers).json()
    assert beyond == {"items": [], "total": 5}

    for bad in ("limit=0", "limit=1001", "offset=-1"):
        assert client.get(f"/v1/uploads/{upload_id}/chunks?{bad}", headers=auth_headers).status_code == 422
    assert client.get(f"/v1/uploads/{upload_id}/chunks", headers=OTHER_USER).status_code == 404


# ---- stats


def test_stats_reflect_recent_uploads(client, auth_headers, monkeypatch) -> None:
    before = client.get("/v1/stats", headers=auth_headers).json()
    assert before["chunks_last_5m"] == 0
    assert before["storage_write_ms_p50"] is None

    upload_id = _init(client, auth_headers, file_size=12, chunk_size=4)
    _put(client, auth_headers, upload_id, 0, b"aaaa")
    _put(client, auth_headers, upload_id, 1, b"bbbb")
    monkeypatch.setattr(executor, "queue_maxsize", 0)  # the next chunk is throttled
    throttled = client.put(f"/v1/uploads/{upload_id}/chunks/2", content=b"cccc", headers=auth_headers)
    assert throttled.status_code == 429

    stats = client.get("/v1/stats", headers=auth_headers).json()
    assert stats["instance_id"] == INSTANCE_ID
    assert stats["app_version"] == settings.app_version
    assert stats["uptime_s"] >= 0
    assert stats["window_s"] == 300
    assert stats["chunks_last_5m"] == 2
    assert stats["bytes_last_5m"] == 8
    assert stats["throttled_last_5m"] == 1
    assert stats["storage_write_ms_p50"] is not None
    assert stats["storage_write_ms_p95"] >= stats["storage_write_ms_p50"]
    assert stats["inflight_chunks"] == 0
    assert stats["queued_chunks"] == 0
    assert stats["inflight_limit"] == executor.global_inflight_limit
    assert stats["queue_limit"] == 0
    assert stats["worker_threads"] == executor.workers


def test_stats_require_authentication(client) -> None:
    assert client.get("/v1/stats").status_code == 401
