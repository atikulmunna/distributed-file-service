"""How the API answers when object storage fails: retry transient errors with backoff, then 503
with Retry-After; fail fast with a generic 500 on permanent errors. Internal details stay in logs."""

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import func, select, update

from app.config import settings
from app.db import SessionLocal
from app.main import upload_limiter
from app.models import Chunk, Upload
from app.storage import storage


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(settings, "retry_base_delay_ms", 1, raising=False)
    monkeypatch.setattr(settings, "retry_max_delay_ms", 2, raising=False)


def _new_upload(client, headers, file_size: int = 10, chunk_size: int = 5) -> str:
    response = client.post(
        "/v1/uploads/init",
        json={"file_name": "f.bin", "file_size": file_size, "chunk_size": chunk_size},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["upload_id"]


def _counting(exc_factory, succeed_after: int | None = None, real=None):
    calls = {"n": 0}

    def fn(*args, **kwargs):
        calls["n"] += 1
        if succeed_after is not None and calls["n"] > succeed_after:
            return real(*args, **kwargs)
        raise exc_factory()

    return fn, calls


def _chunk_count(upload_id: str) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Chunk).where(Chunk.upload_id == upload_id))


def test_storage_outage_returns_503_with_retry_after(client, auth_headers, monkeypatch) -> None:
    upload_id = _new_upload(client, auth_headers)
    failing, calls = _counting(lambda: EndpointConnectionError(endpoint_url="http://internal-s3.example:9000"))
    monkeypatch.setattr(storage, "write_chunk", failing)

    response = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"aaaaa", headers=auth_headers)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    assert "internal-s3" not in response.text
    assert calls["n"] == settings.max_retries + 1
    assert _chunk_count(upload_id) == 0
    assert upload_limiter._counts.get(upload_id, 0) == 0


def test_permanent_storage_error_returns_500_without_internal_detail(client, auth_headers, monkeypatch) -> None:
    upload_id = _new_upload(client, auth_headers)
    error = ClientError(
        {
            "Error": {"Code": "AccessDenied", "Message": "no access to secret-bucket-name"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "PutObject",
    )
    failing, calls = _counting(lambda: error)
    monkeypatch.setattr(storage, "write_chunk", failing)

    response = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"aaaaa", headers=auth_headers)

    assert response.status_code == 500
    assert "secret-bucket-name" not in response.text
    assert "AccessDenied" not in response.text
    assert calls["n"] == 1  # permanent errors are not retried
    assert _chunk_count(upload_id) == 0


def test_transient_error_then_success_records_the_retry(client, auth_headers, monkeypatch) -> None:
    upload_id = _new_upload(client, auth_headers)
    flaky, calls = _counting(
        lambda: EndpointConnectionError(endpoint_url="http://s3"), succeed_after=1, real=storage.write_chunk
    )
    monkeypatch.setattr(storage, "write_chunk", flaky)

    response = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"aaaaa", headers=auth_headers)

    assert response.status_code == 202
    assert calls["n"] == 2
    with SessionLocal() as db:
        assert db.scalar(select(Chunk.retry_count).where(Chunk.upload_id == upload_id)) == 1


def test_init_during_storage_outage_returns_503(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(settings, "storage_backend", "s3")
    failing, _ = _counting(lambda: EndpointConnectionError(endpoint_url="http://s3"))
    monkeypatch.setattr(storage, "initialize_upload", failing)

    response = client.post(
        "/v1/uploads/init",
        json={"file_name": "mp.bin", "file_size": 12 * 1024 * 1024, "chunk_size": 5 * 1024 * 1024},
        headers=auth_headers,
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Upload)) == 0


def test_complete_during_storage_outage_returns_503_and_releases_the_claim(client, auth_headers, monkeypatch) -> None:
    upload_id = _new_upload(client, auth_headers)
    for index, body in enumerate((b"aaaaa", b"bbbbb")):
        assert (
            client.put(f"/v1/uploads/{upload_id}/chunks/{index}", content=body, headers=auth_headers).status_code == 202
        )
    with SessionLocal() as db:  # take the multipart completion path
        db.execute(update(Upload).where(Upload.id == upload_id).values(multipart_upload_id="mp-test"))
        db.execute(update(Chunk).where(Chunk.upload_id == upload_id).values(s3_etag='"etag"'))
        db.commit()
    monkeypatch.setattr(settings, "storage_backend", "s3")
    failing, calls = _counting(lambda: EndpointConnectionError(endpoint_url="http://s3"))
    monkeypatch.setattr(storage, "complete_upload", failing)
    monkeypatch.setattr(storage, "object_exists", lambda key: False)

    response = client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    assert calls["n"] == settings.max_retries + 1
    with SessionLocal() as db:
        assert db.get(Upload, upload_id).status == "IN_PROGRESS"
