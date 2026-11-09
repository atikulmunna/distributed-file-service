"""Chunk writes under concurrency: duplicate writers, racing completion, and failed commits."""

import hashlib
import threading

from db_hooks import before_statement, db_now, raw_insert
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.db import SessionLocal
from app.main import app, upload_limiter
from app.models import Chunk, ChunkRequestIdempotency, Upload
from app.storage import storage


def _new_upload(client, headers, file_size: int, chunk_size: int) -> str:
    response = client.post(
        "/v1/uploads/init", json={"file_name": "f.bin", "file_size": file_size, "chunk_size": chunk_size}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["upload_id"]


def _chunk_rows(upload_id: str, index: int = 0) -> list[Chunk]:
    with SessionLocal() as db:
        return list(db.scalars(select(Chunk).where(Chunk.upload_id == upload_id, Chunk.chunk_index == index)))


def test_concurrent_same_chunk_puts_succeed(client, auth_headers) -> None:
    """Another request commits the same chunk between our existence check and our insert."""
    upload_id = _new_upload(client, auth_headers, 10, 5)
    body = b"abcde"

    def racing_chunk_row(conn, cursor, parameters) -> None:
        stamp = db_now(conn)
        raw_insert(
            conn,
            cursor,
            "chunks",
            ["upload_id", "chunk_index", "size_bytes", "s3_key", "status", "retry_count", "created_at", "updated_at"],
            (upload_id, 0, 5, f"uploads/{upload_id}/chunk_0", "UPLOADED", 0, stamp, stamp),
        )

    with before_statement("INSERT INTO chunks ", racing_chunk_row) as injection:
        response = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=body, headers=auth_headers)

    assert injection.fired, "the racing insert never ran"
    assert response.status_code == 202, response.text
    rows = _chunk_rows(upload_id)
    assert len(rows) == 1
    assert rows[0].chunk_checksum_sha256 == hashlib.sha256(body).hexdigest()


def test_parallel_puts_of_same_chunk_all_succeed(client, auth_headers, slow_storage) -> None:
    slow_storage(0.2)  # the writers overlap in storage, then record at about the same time
    upload_id = _new_upload(client, auth_headers, 10, 5)
    statuses: list[int] = []

    def put() -> None:
        statuses.append(client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"abcde", headers=auth_headers).status_code)

    threads = [threading.Thread(target=put) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert statuses == [202] * 4
    assert len(_chunk_rows(upload_id)) == 1


def test_chunk_put_after_complete_is_rejected(client, auth_headers, monkeypatch) -> None:
    upload_id = _new_upload(client, auth_headers, 10, 5)
    assert client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"aaaaa", headers=auth_headers).status_code == 202
    real_write = storage.write_chunk

    def write_while_upload_completes(*args, **kwargs):
        with SessionLocal() as db:  # another request completes the upload during this storage write
            db.execute(update(Upload).where(Upload.id == upload_id).values(status="COMPLETED"))
            db.commit()
        return real_write(*args, **kwargs)

    monkeypatch.setattr(storage, "write_chunk", write_while_upload_completes)
    response = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"zzzzz", headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["detail"] == "upload is not accepting chunks"
    rows = _chunk_rows(upload_id)
    assert len(rows) == 1
    assert rows[0].chunk_checksum_sha256 == hashlib.sha256(b"aaaaa").hexdigest()
    assert upload_limiter._counts.get(upload_id, 0) == 0


def test_commit_failure_after_write_leaves_chunk_missing(client, auth_headers) -> None:
    """The process dies (here: the DB write fails) after the storage write but before the commit."""
    upload_id = _new_upload(client, auth_headers, 10, 5)

    def fail(conn, cursor, parameters) -> None:
        raise RuntimeError("simulated crash between storage write and DB commit")

    with before_statement("INSERT INTO chunks ", fail) as injection:
        with TestClient(app, raise_server_exceptions=False) as tolerant:
            first = tolerant.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"aaaaa", headers=auth_headers)
    assert injection.fired
    assert first.status_code == 500
    assert _chunk_rows(upload_id) == []
    assert upload_limiter._counts.get(upload_id, 0) == 0

    missing = client.get(f"/v1/uploads/{upload_id}/missing-chunks", headers=auth_headers).json()
    assert missing["missing_chunk_indexes"] == [0, 1]
    assert client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"aaaaa", headers=auth_headers).status_code == 202
    assert len(_chunk_rows(upload_id)) == 1


def _racing_idempotency_row(upload_id: str, key: str, fingerprint: str):
    def insert(conn, cursor, parameters) -> None:
        raw_insert(
            conn,
            cursor,
            "chunk_request_idempotency",
            ["upload_id", "chunk_index", "idempotency_key", "request_fingerprint", "created_at"],
            (upload_id, 0, key, fingerprint, db_now(conn)),
        )

    return insert


def test_racing_same_idempotency_key_same_payload_succeeds(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 10, 5)
    body = b"abcde"
    headers = {**auth_headers, "Idempotency-Key": "k1"}
    racing = _racing_idempotency_row(upload_id, "k1", hashlib.sha256(body).hexdigest())

    with before_statement("INSERT INTO chunk_request_idempotency ", racing) as injection:
        response = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=body, headers=headers)

    assert injection.fired
    assert response.status_code == 202, response.text
    with SessionLocal() as db:
        keys = list(db.scalars(select(ChunkRequestIdempotency).where(ChunkRequestIdempotency.upload_id == upload_id)))
    assert len(keys) == 1


def test_racing_idempotency_key_with_different_payload_conflicts(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 10, 5)
    headers = {**auth_headers, "Idempotency-Key": "k1"}
    racing = _racing_idempotency_row(upload_id, "k1", hashlib.sha256(b"other").hexdigest())

    with before_statement("INSERT INTO chunk_request_idempotency ", racing) as injection:
        response = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"abcde", headers=headers)

    assert injection.fired
    assert response.status_code == 409
    assert response.json()["detail"] == "idempotency key reused with different chunk payload"
    assert _chunk_rows(upload_id) == []  # the chunk write rolled back with the conflicting key
