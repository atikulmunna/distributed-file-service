"""Asynchronous whole-file verification: complete returns 202 VERIFYING, a worker hashes the stored
file and moves the upload to COMPLETED or FAILED."""

import hashlib
import time
from datetime import datetime, timedelta, timezone

from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.config import settings
from app.db import SessionLocal, engine
from app.finalizer import handle, process, sweep_lost_jobs
from app.jobs import FinalizeJob, job_queue
from app.main import app
from app.models import Chunk, Upload
from app.storage import storage

DATA = b"aaaaabbbbb"


def _upload(client, headers, data: bytes = DATA, checksum: str | None = None) -> str:
    payload = {"file_name": "f.bin", "file_size": len(data), "chunk_size": 5}
    if checksum is not None:
        payload["file_checksum_sha256"] = checksum
    init = client.post("/v1/uploads/init", json=payload, headers=headers)
    assert init.status_code == 201, init.text
    upload_id = init.json()["upload_id"]
    for index in range(0, len(data), 5):
        assert client.put(f"/v1/uploads/{upload_id}/chunks/{index // 5}", content=data[index : index + 5], headers=headers).status_code == 202
    return upload_id


def _verifying(client, headers, checksum: str) -> str:
    upload_id = _upload(client, headers, checksum=checksum)
    response = client.post(f"/v1/uploads/{upload_id}/complete", headers=headers)
    assert response.status_code == 202, response.text
    assert response.json() == {"upload_id": upload_id, "status": "VERIFYING"}
    return upload_id


def _state(upload_id: str) -> tuple[str, str | None]:
    with SessionLocal() as db:
        upload = db.get(Upload, upload_id)
        return upload.status, upload.failure_reason


GOOD = hashlib.sha256(DATA).hexdigest()
BAD = hashlib.sha256(b"something else").hexdigest()


# ---------------------------------------------------------------- complete


def test_complete_without_checksum_is_synchronous(client, auth_headers) -> None:
    upload_id = _upload(client, auth_headers)
    response = client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"


def test_complete_with_checksum_returns_202_verifying(client, auth_headers) -> None:
    upload_id = _verifying(client, auth_headers, GOOD)
    assert _state(upload_id) == ("VERIFYING", None)
    reservation = job_queue.reserve(timeout_s=0.1)
    assert reservation is not None and reservation.job == FinalizeJob(upload_id)


def test_complete_replay_while_verifying_returns_202(client, auth_headers) -> None:
    upload_id = _verifying(client, auth_headers, GOOD)
    replay = client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert replay.status_code == 202
    assert replay.json()["status"] == "VERIFYING"


def test_download_while_verifying_is_409(client, auth_headers) -> None:
    upload_id = _verifying(client, auth_headers, GOOD)
    response = client.get(f"/v1/uploads/{upload_id}/download", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "verification in progress"


def test_status_endpoint(client, auth_headers) -> None:
    upload_id = _verifying(client, auth_headers, BAD)
    response = client.get(f"/v1/uploads/{upload_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {"upload_id": upload_id, "status": "VERIFYING", "failure_reason": None}
    process(FinalizeJob(upload_id))
    body = client.get(f"/v1/uploads/{upload_id}", headers=auth_headers).json()
    assert body["status"] == "FAILED" and body["failure_reason"].startswith("file checksum mismatch")
    assert client.get(f"/v1/uploads/{upload_id}", headers={"X-API-Key": "other-key"}).status_code == 404


# ---------------------------------------------------------------- the worker


def test_finalizer_marks_completed_on_match(client, auth_headers) -> None:
    upload_id = _verifying(client, auth_headers, GOOD)
    process(FinalizeJob(upload_id))
    assert _state(upload_id) == ("COMPLETED", None)
    assert client.get(f"/v1/uploads/{upload_id}/download", headers=auth_headers).content == DATA


def test_finalizer_marks_failed_on_mismatch_with_reason(client, auth_headers) -> None:
    upload_id = _verifying(client, auth_headers, BAD)
    process(FinalizeJob(upload_id))
    status, reason = _state(upload_id)
    assert status == "FAILED"
    assert reason == f"file checksum mismatch: expected {BAD}, got {GOOD}"


def test_finalizer_is_idempotent(client, auth_headers) -> None:
    upload_id = _verifying(client, auth_headers, GOOD)
    process(FinalizeJob(upload_id))
    process(FinalizeJob(upload_id))  # a redelivered job after success changes nothing
    assert _state(upload_id) == ("COMPLETED", None)


def test_finalizer_skips_uploads_that_are_not_verifying(client, auth_headers) -> None:
    upload_id = _upload(client, auth_headers)
    process(FinalizeJob(upload_id))
    process(FinalizeJob("no-such-upload"))
    assert _state(upload_id) == ("IN_PROGRESS", None)


def test_finalizer_verifies_the_assembled_object_for_multipart(client, auth_headers, monkeypatch) -> None:
    upload_id = _upload(client, auth_headers, checksum=GOOD)
    with SessionLocal() as db:
        db.execute(update(Upload).where(Upload.id == upload_id).values(multipart_upload_id="mp-test"))
        db.execute(update(Chunk).where(Chunk.upload_id == upload_id).values(s3_etag='"etag"'))
        db.commit()
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(storage, "complete_upload", lambda **kwargs: None)
    assert client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code == 202
    read_keys: list[str] = []

    def stream_assembled(key: str, start: int, end: int, block_size: int = 8 * 1024 * 1024):
        read_keys.append(key)
        yield DATA[start : end + 1]

    monkeypatch.setattr(storage, "iter_range", stream_assembled)
    process(FinalizeJob(upload_id))
    assert read_keys == [f"uploads/{upload_id}/assembled"]
    assert _state(upload_id) == ("COMPLETED", None)


def test_finalizer_holds_no_db_connection_while_hashing(client, auth_headers, monkeypatch) -> None:
    upload_id = _verifying(client, auth_headers, GOOD)
    held: list[int] = []
    real = storage.iter_range

    def observed(key: str, start: int, end: int, block_size: int = 8 * 1024 * 1024):
        held.append(engine.pool.checkedout())
        yield from real(key, start, end, block_size)

    monkeypatch.setattr(storage, "iter_range", observed)
    process(FinalizeJob(upload_id))
    assert held and set(held) == {0}


def test_worker_retries_a_storage_failure_then_completes(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(settings, "retry_base_delay_ms", 1)
    monkeypatch.setattr(settings, "max_retries", 0)
    upload_id = _verifying(client, auth_headers, GOOD)
    real = storage.iter_range
    failures = {"left": 1}

    def flaky(key: str, start: int, end: int, block_size: int = 8 * 1024 * 1024):
        if failures["left"]:
            failures["left"] -= 1
            raise EndpointConnectionError(endpoint_url="http://s3")
        yield from real(key, start, end, block_size)

    monkeypatch.setattr(storage, "iter_range", flaky)
    first = job_queue.reserve(timeout_s=0.1)
    handle(job_queue, first, retry_delay_s=0)
    assert _state(upload_id)[0] == "VERIFYING"  # released for another attempt, not failed
    second = job_queue.reserve(timeout_s=0.5)
    assert second is not None and second.job == FinalizeJob(upload_id, attempt=1)
    handle(job_queue, second, retry_delay_s=0)
    assert _state(upload_id) == ("COMPLETED", None)
    assert job_queue.reserve(timeout_s=0.05) is None


def test_worker_gives_up_after_max_attempts(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(settings, "finalizer_max_attempts", 2)
    monkeypatch.setattr(settings, "max_retries", 0)
    upload_id = _verifying(client, auth_headers, GOOD)

    def broken(key: str, start: int, end: int, block_size: int = 8 * 1024 * 1024):
        raise OSError("stored object unreadable")
        yield b""  # pragma: no cover

    monkeypatch.setattr(storage, "iter_range", broken)
    handle(job_queue, job_queue.reserve(timeout_s=0.1), retry_delay_s=0)
    handle(job_queue, job_queue.reserve(timeout_s=0.5), retry_delay_s=0)
    status, reason = _state(upload_id)
    assert status == "FAILED"
    assert reason.startswith("verification failed after 2 attempts")
    assert job_queue.reserve(timeout_s=0.05) is None


def test_lost_job_is_requeued_by_sweep(client, auth_headers) -> None:
    upload_id = _verifying(client, auth_headers, GOOD)
    job_queue.clear()  # the enqueue was lost (for example the queue was down)
    with SessionLocal() as db:
        stale = datetime.now(timezone.utc) - timedelta(seconds=settings.job_lease_seconds + 60)
        db.execute(update(Upload).where(Upload.id == upload_id).values(updated_at=stale))
        db.commit()
    assert sweep_lost_jobs(job_queue) == 1
    reservation = job_queue.reserve(timeout_s=0.1)
    assert reservation is not None and reservation.job.upload_id == upload_id
    assert sweep_lost_jobs(job_queue) == 0  # the sweep touched it, so it is not requeued again


def test_in_process_worker_completes_verification_end_to_end(auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(settings, "finalizer_in_process", True)
    with TestClient(app) as client:
        upload_id = _verifying(client, auth_headers, GOOD)
        deadline = time.monotonic() + 5
        while client.get(f"/v1/uploads/{upload_id}", headers=auth_headers).json()["status"] == "VERIFYING":
            assert time.monotonic() < deadline, "verification did not finish"
            time.sleep(0.05)
        assert client.get(f"/v1/uploads/{upload_id}", headers=auth_headers).json()["status"] == "COMPLETED"
