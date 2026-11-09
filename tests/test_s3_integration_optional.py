"""Integration tests against a real S3-compatible endpoint (Versity Gateway locally and in CI, or AWS).

Enable with RUN_S3_INTEGRATION=1. Settings:
  S3_INTEGRATION_ENDPOINT_URL  e.g. http://localhost:7070 (empty: AWS)
  S3_INTEGRATION_BUCKET        default dfs-it (created if missing)
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION  standard credentials
SSE is not covered here (Versity does not implement it); the fake-client unit tests cover it.
"""

import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import update

import app.chunk_service as chunk_service
import app.maintenance as maintenance
import app.routes.uploads as upload_routes
import app.upload_lifecycle as upload_lifecycle
from app.config import settings
from app.db import SessionLocal
from app.models import Upload
from app.storage import S3ChunkStorage
from scripts.bench_init import ensure_bucket

ENDPOINT = os.getenv("S3_INTEGRATION_ENDPOINT_URL", "")
BUCKET = os.getenv("S3_INTEGRATION_BUCKET", "dfs-it")
MIB = 1024 * 1024

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_S3_INTEGRATION") != "1", reason="Set RUN_S3_INTEGRATION=1 (see module docstring)."
)


@pytest.fixture
def s3() -> S3ChunkStorage:
    storage = S3ChunkStorage(
        bucket=BUCKET,
        region=os.getenv("AWS_REGION", "us-east-1"),
        endpoint_url=ENDPOINT or None,
        path_style=bool(ENDPOINT),
    )
    ensure_bucket(storage.client, BUCKET)
    return storage


@pytest.fixture
def s3_backend(monkeypatch, s3):
    """Point the running app at the real S3 endpoint (the test harness defaults to local storage)."""
    monkeypatch.setattr(settings, "storage_backend", "s3")
    for module in (upload_routes, chunk_service, upload_lifecycle, maintenance):
        monkeypatch.setattr(module, "storage", s3)
    return s3


def _objects(s3: S3ChunkStorage, upload_id: str) -> dict[str, int]:
    listing = s3.client.list_objects_v2(Bucket=BUCKET, Prefix=f"uploads/{upload_id}/")
    return {item["Key"]: item["Size"] for item in listing.get("Contents", [])}


def _pending_multipart(s3: S3ChunkStorage, upload_id: str) -> list[dict]:
    uploads = s3.client.list_multipart_uploads(Bucket=BUCKET).get("Uploads", [])
    return [u for u in uploads if u["Key"] == s3.assembled_key(upload_id)]


def _cleanup(s3: S3ChunkStorage, upload_id: str) -> None:
    for key in _objects(s3, upload_id):
        s3.delete_key(key)


def _upload(client, headers, data: bytes, chunk_size: int, with_checksum: bool = True) -> str:
    payload = {"file_name": "it.bin", "file_size": len(data), "chunk_size": chunk_size}
    if with_checksum:
        payload["file_checksum_sha256"] = hashlib.sha256(data).hexdigest()
    init = client.post("/v1/uploads/init", json=payload, headers=headers)
    assert init.status_code == 201, init.text
    upload_id = init.json()["upload_id"]
    for index in range(0, len(data), chunk_size):
        piece = data[index : index + chunk_size]
        response = client.put(
            f"/v1/uploads/{upload_id}/chunks/{index // chunk_size}",
            content=piece,
            headers={**headers, "X-Chunk-SHA256": hashlib.sha256(piece).hexdigest()},
        )
        assert response.status_code == 202, response.text
    return upload_id


def test_s3compat_multipart_round_trip(client, auth_headers, s3_backend) -> None:
    data = os.urandom(3 * 5 * MIB + MIB)  # three full 5 MiB parts plus a 1 MiB last part
    upload_id = _upload(client, auth_headers, data, 5 * MIB)
    try:
        complete = client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)
        assert complete.status_code == 200, complete.text

        download = client.get(f"/v1/uploads/{upload_id}/download", headers=auth_headers)
        assert download.status_code == 200
        assert hashlib.sha256(download.content).hexdigest() == hashlib.sha256(data).hexdigest()
        ranged = client.get(f"/v1/uploads/{upload_id}/download", headers={**auth_headers, "Range": "bytes=5242870-5242890"})
        assert ranged.status_code == 206 and ranged.content == data[5242870:5242891]

        # one write per chunk: the only object is the assembled file, exactly the file's size
        assert _objects(s3_backend, upload_id) == {s3_backend.assembled_key(upload_id): len(data)}
        assert _pending_multipart(s3_backend, upload_id) == []
    finally:
        _cleanup(s3_backend, upload_id)


def test_s3compat_rejects_bad_part_checksum(s3) -> None:
    upload_id = f"it-bad-{os.urandom(4).hex()}"
    multipart_upload_id = s3.initialize_upload(upload_id)
    try:
        with pytest.raises(ClientError) as raised:
            s3.write_chunk(upload_id, 0, b"x" * MIB, multipart_upload_id, sha256_hex=hashlib.sha256(b"other").hexdigest())
        assert raised.value.response["Error"]["Code"] in ("BadDigest", "InvalidDigest", "XAmzContentChecksumMismatch")
    finally:
        s3.abort_upload(upload_id, multipart_upload_id)


def test_s3compat_abort_discards_everything(client, auth_headers, s3_backend) -> None:
    data = os.urandom(2 * 5 * MIB)
    init = client.post(
        "/v1/uploads/init", json={"file_name": "abort.bin", "file_size": len(data), "chunk_size": 5 * MIB}, headers=auth_headers
    )
    upload_id = init.json()["upload_id"]
    assert client.put(f"/v1/uploads/{upload_id}/chunks/0", content=data[: 5 * MIB], headers=auth_headers).status_code == 202
    assert len(_pending_multipart(s3_backend, upload_id)) == 1

    assert client.delete(f"/v1/uploads/{upload_id}", headers=auth_headers).status_code == 200
    assert _pending_multipart(s3_backend, upload_id) == []
    assert _objects(s3_backend, upload_id) == {}


def test_s3compat_object_exists(s3) -> None:
    key = s3.write_chunk(f"it-exists-{os.urandom(4).hex()}", 0, b"x").key
    try:
        assert s3.object_exists(key) is True
        assert s3.object_exists(key + "-missing") is False
    finally:
        s3.delete_key(key)


def test_s3compat_completion_retry_after_assembly_succeeds(client, auth_headers, s3_backend, monkeypatch) -> None:
    """The first completion assembled the file, then the process died before COMPLETED was recorded.
    On real storage the second CompleteMultipartUpload fails; the assembled object proves success."""
    data = os.urandom(2 * 5 * MIB)
    upload_id = _upload(client, auth_headers, data, 5 * MIB, with_checksum=False)
    try:
        assert client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers).status_code == 200
        with SessionLocal() as db:  # rewind to "completing, holder died a while ago"
            db.execute(
                update(Upload)
                .where(Upload.id == upload_id)
                .values(status="COMPLETING", updated_at=datetime.now(timezone.utc) - timedelta(minutes=5))
            )
            db.commit()
        retry = client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)
        assert retry.status_code == 200, retry.text
        assert retry.json()["status"] == "COMPLETED"
    finally:
        _cleanup(s3_backend, upload_id)
