import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import Base, SessionLocal, engine
from app.main import app, upload_limiter
from app.models import Chunk, ChunkRequestIdempotency, CompleteRequestIdempotency, InitRequestIdempotency, Upload
from app.worker import executor

AUTH_HEADERS = {"X-API-Key": "dev-key"}


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
    shutil.rmtree(Path("data"), ignore_errors=True)


def _init_upload(client: TestClient, file_size: int, chunk_size: int, headers: dict | None = None) -> str:
    merged_headers = dict(AUTH_HEADERS)
    if headers:
        merged_headers.update(headers)
    response = client.post(
        "/v1/uploads/init",
        json={"file_name": "sample.bin", "file_size": file_size, "chunk_size": chunk_size},
        headers=merged_headers,
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["total_chunks"] >= 1
    return payload["upload_id"]


def test_upload_resume_complete_download_flow() -> None:
    _reset_state()
    with TestClient(app) as client:
        client.headers.update(AUTH_HEADERS)
        upload_id = _init_upload(client, file_size=11, chunk_size=4)

        missing = client.get(f"/v1/uploads/{upload_id}/missing-chunks")
        assert missing.status_code == 200
        assert missing.json()["missing_chunk_indexes"] == [0, 1, 2]

        for idx, chunk in enumerate((b"abcd", b"efgh", b"ijk")):
            response = client.put(
                f"/v1/uploads/{upload_id}/chunks/{idx}",
                content=chunk,
                headers={"Content-Length": str(len(chunk))},
            )
            assert response.status_code == 202, response.text

        complete = client.post(f"/v1/uploads/{upload_id}/complete")
        assert complete.status_code == 200
        assert complete.json()["status"] == "COMPLETED"

        download = client.get(f"/v1/uploads/{upload_id}/download")
        assert download.status_code == 200
        assert download.content == b"abcdefghijk"

        partial = client.get(f"/v1/uploads/{upload_id}/download", headers={"Range": "bytes=2-7"})
        assert partial.status_code == 206
        assert partial.content == b"cdefgh"
        assert partial.headers["Content-Range"] == "bytes 2-7/11"


def test_complete_rejects_when_chunks_missing() -> None:
    _reset_state()
    with TestClient(app) as client:
        client.headers.update(AUTH_HEADERS)
        upload_id = _init_upload(client, file_size=8, chunk_size=4)
        response = client.put(
            f"/v1/uploads/{upload_id}/chunks/0",
            content=b"abcd",
            headers={"Content-Length": "4"},
        )
        assert response.status_code == 202

        incomplete = client.post(f"/v1/uploads/{upload_id}/complete")
        assert incomplete.status_code == 409
        assert "missing chunks" in incomplete.text


def test_metrics_endpoint_available() -> None:
    _reset_state()
    with TestClient(app) as client:
        client.headers.update(AUTH_HEADERS)
        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "chunks_uploaded_total" in metrics.text


def test_init_idempotency_key_reuses_upload() -> None:
    _reset_state()
    with TestClient(app) as client:
        client.headers.update(AUTH_HEADERS)
        headers = {"Idempotency-Key": "init-req-1"}
        first = client.post(
            "/v1/uploads/init",
            json={"file_name": "idempotent.bin", "file_size": 9, "chunk_size": 3},
            headers=headers,
        )
        second = client.post(
            "/v1/uploads/init",
            json={"file_name": "idempotent.bin", "file_size": 9, "chunk_size": 3},
            headers=headers,
        )
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["upload_id"] == second.json()["upload_id"]

        with SessionLocal() as db:
            total_uploads = db.query(Upload).count()
            assert total_uploads == 1


def test_chunk_idempotency_key_replay_does_not_duplicate_rows() -> None:
    _reset_state()
    with TestClient(app) as client:
        client.headers.update(AUTH_HEADERS)
        upload_id = _init_upload(client, file_size=4, chunk_size=4)
        headers = {"Content-Length": "4", "Idempotency-Key": "chunk-req-1"}

        first = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"abcd", headers=headers)
        second = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"abcd", headers=headers)

        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json()["status"] == "UPLOADED"

        with SessionLocal() as db:
            total_chunks = db.query(Chunk).filter(Chunk.upload_id == upload_id).count()
            total_chunk_idempotency = (
                db.query(ChunkRequestIdempotency)
                .filter(ChunkRequestIdempotency.upload_id == upload_id)
                .count()
            )
            assert total_chunks == 1
            assert total_chunk_idempotency == 1


def test_upload_status_transitions_and_upload_block_after_complete() -> None:
    _reset_state()
    with TestClient(app) as client:
        client.headers.update(AUTH_HEADERS)
        upload_id = _init_upload(client, file_size=4, chunk_size=4)

        with SessionLocal() as db:
            upload = db.get(Upload, upload_id)
            assert upload is not None
            assert upload.status == "INITIATED"

        upload_chunk = client.put(
            f"/v1/uploads/{upload_id}/chunks/0",
            content=b"abcd",
            headers={"Content-Length": "4"},
        )
        assert upload_chunk.status_code == 202

        with SessionLocal() as db:
            upload = db.get(Upload, upload_id)
            assert upload is not None
            assert upload.status == "IN_PROGRESS"

        complete = client.post(f"/v1/uploads/{upload_id}/complete")
        assert complete.status_code == 200
        assert complete.json()["status"] == "COMPLETED"

        late_chunk = client.put(
            f"/v1/uploads/{upload_id}/chunks/0",
            content=b"zzzz",
            headers={"Content-Length": "4"},
        )
        assert late_chunk.status_code == 409


def test_complete_idempotency_replay() -> None:
    _reset_state()
    with TestClient(app) as client:
        client.headers.update(AUTH_HEADERS)
        upload_id = _init_upload(client, file_size=4, chunk_size=4)
        upload_chunk = client.put(
            f"/v1/uploads/{upload_id}/chunks/0",
            content=b"abcd",
            headers={"Content-Length": "4"},
        )
        assert upload_chunk.status_code == 202

        headers = {"Idempotency-Key": "complete-req-1"}
        first = client.post(f"/v1/uploads/{upload_id}/complete", headers=headers)
        second = client.post(f"/v1/uploads/{upload_id}/complete", headers=headers)
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["status"] == "COMPLETED"
        assert second.json()["status"] == "COMPLETED"

        with SessionLocal() as db:
            total_complete_idempotency = (
                db.query(CompleteRequestIdempotency)
                .filter(CompleteRequestIdempotency.upload_id == upload_id)
                .count()
            )
            assert total_complete_idempotency == 1


def test_throttled_queue_full_returns_retry_headers() -> None:
    _reset_state()
    old_queue_maxsize = executor.queue_maxsize
    executor.queue_maxsize = 0
    try:
        with TestClient(app) as client:
            client.headers.update(AUTH_HEADERS)
            upload_id = _init_upload(client, file_size=4, chunk_size=4)
            response = client.put(
                f"/v1/uploads/{upload_id}/chunks/0",
                content=b"abcd",
                headers={"Content-Length": "4"},
            )
            assert response.status_code == 429
            assert response.headers.get("Retry-After") == "1"
            assert response.headers.get("X-RateLimit-Reason") == "queue_full"
    finally:
        executor.queue_maxsize = old_queue_maxsize


def test_throttled_per_upload_limit_returns_retry_headers() -> None:
    _reset_state()
    old_limit = upload_limiter.limit
    old_fair = upload_limiter.fair_share_limit
    upload_limiter.limit = 0
    upload_limiter.fair_share_limit = None
    try:
        with TestClient(app) as client:
            client.headers.update(AUTH_HEADERS)
            upload_id = _init_upload(client, file_size=4, chunk_size=4)
            response = client.put(
                f"/v1/uploads/{upload_id}/chunks/0",
                content=b"abcd",
                headers={"Content-Length": "4"},
            )
            assert response.status_code == 429
            assert response.headers.get("Retry-After") == "1"
            assert response.headers.get("X-RateLimit-Reason") == "upload_inflight_limit"
    finally:
        upload_limiter.limit = old_limit
        upload_limiter.fair_share_limit = old_fair


def test_init_idempotency_conflict_for_different_payload() -> None:
    _reset_state()
    with TestClient(app) as client:
        client.headers.update(AUTH_HEADERS)
        headers = {"Idempotency-Key": "init-conflict-1"}
        first = client.post(
            "/v1/uploads/init",
            json={"file_name": "same-key.bin", "file_size": 9, "chunk_size": 3},
            headers=headers,
        )
        second = client.post(
            "/v1/uploads/init",
            json={"file_name": "same-key.bin", "file_size": 10, "chunk_size": 3},
            headers=headers,
        )
        assert first.status_code == 201
        assert second.status_code == 409


def test_chunk_idempotency_conflict_for_different_payload() -> None:
    _reset_state()
    with TestClient(app) as client:
        client.headers.update(AUTH_HEADERS)
        upload_id = _init_upload(client, file_size=4, chunk_size=4)
        headers = {"Content-Length": "4", "Idempotency-Key": "chunk-conflict-1"}
        first = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"abcd", headers=headers)
        second = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"wxyz", headers=headers)
        assert first.status_code == 202
        assert second.status_code == 409


def test_complete_idempotency_conflict_for_different_uploads() -> None:
    _reset_state()
    with TestClient(app) as client:
        client.headers.update(AUTH_HEADERS)
        upload_a = _init_upload(client, file_size=4, chunk_size=4)
        upload_b = _init_upload(client, file_size=4, chunk_size=4)

        for upload_id in (upload_a, upload_b):
            up = client.put(
                f"/v1/uploads/{upload_id}/chunks/0",
                content=b"abcd",
                headers={"Content-Length": "4"},
            )
            assert up.status_code == 202

        headers = {"Idempotency-Key": "complete-conflict-1"}
        first = client.post(f"/v1/uploads/{upload_a}/complete", headers=headers)
        second = client.post(f"/v1/uploads/{upload_b}/complete", headers=headers)
        assert first.status_code == 200
        assert second.status_code == 409
