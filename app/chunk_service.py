"""The chunk upload pipeline: prepare (validate), persist (storage write), record (DB).

Every function here is synchronous and blocking. The route runs them off the event loop,
and each DB step uses its own short session, so no connection is held while a request waits
on the network or on object storage.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthUser
from app.db import SessionLocal, dialect_insert
from app.layout import ChunkLayout
from app.metrics import (
    bytes_uploaded_total,
    chunks_uploaded_total,
    db_update_latency_seconds,
    s3_put_latency_seconds,
)
from app.models import (
    Chunk,
    ChunkRequestIdempotency,
    ChunkStatus,
    Upload,
    UploadStatus,
    utc_now,
)
from app.schemas import UploadChunkResponse
from app.storage import StorageWriteResult, storage
from app.upload_access import get_owned_upload


@dataclass(frozen=True)
class ChunkPlan:
    upload_id: str
    chunk_index: int
    multipart_upload_id: str | None
    size: int
    sha256: str


def admit_chunk(upload_id: str, chunk_index: int, user: AuthUser) -> int:
    """Check ownership, state, and index before any body is read. Returns the exact body size
    this chunk must have under the upload's layout."""
    with SessionLocal() as db:
        upload = get_owned_upload(db, upload_id, user)
        if upload.status not in (UploadStatus.in_progress.value, UploadStatus.initiated.value):
            raise HTTPException(status_code=409, detail="upload is not accepting chunks")
        try:
            return ChunkLayout(upload.file_size, upload.chunk_size).expected_size(chunk_index)
        except IndexError:
            raise HTTPException(status_code=400, detail="chunk index out of bounds") from None


def prepare_chunk(
    upload_id: str,
    chunk_index: int,
    body: bytes,
    claimed_sha256: str | None,
    idempotency_key: str | None,
    user: AuthUser,
    declared_length: int = 0,
) -> ChunkPlan | UploadChunkResponse:
    """Validate a chunk request. Returns the stored response for an idempotent replay."""
    with SessionLocal() as db:
        upload = get_owned_upload(db, upload_id, user)
        if upload.status not in (UploadStatus.in_progress.value, UploadStatus.initiated.value):
            raise HTTPException(status_code=409, detail="upload is not accepting chunks")
        if chunk_index < 0 or chunk_index >= upload.total_chunks:
            raise HTTPException(status_code=400, detail="chunk index out of bounds")
        if not body:
            raise HTTPException(status_code=400, detail="chunk payload is empty")
        if declared_length and declared_length != len(body):
            raise HTTPException(status_code=400, detail="content-length mismatch")

        chunk_fingerprint = hashlib.sha256(body).hexdigest()
        if claimed_sha256 and claimed_sha256.lower() != chunk_fingerprint:
            raise HTTPException(status_code=400, detail="chunk checksum mismatch")
        if idempotency_key:
            existing_request = db.scalar(
                select(ChunkRequestIdempotency).where(
                    ChunkRequestIdempotency.upload_id == upload_id,
                    ChunkRequestIdempotency.chunk_index == chunk_index,
                    ChunkRequestIdempotency.idempotency_key == idempotency_key,
                )
            )
            if existing_request:
                if existing_request.request_fingerprint != chunk_fingerprint:
                    raise HTTPException(status_code=409, detail="idempotency key reused with different chunk payload")
                existing_chunk = db.scalar(
                    select(Chunk).where(Chunk.upload_id == upload_id, Chunk.chunk_index == chunk_index)
                )
                if existing_chunk and existing_chunk.status == ChunkStatus.uploaded.value:
                    return UploadChunkResponse(upload_id=upload_id, chunk_index=chunk_index, status=existing_chunk.status)

        return ChunkPlan(
            upload_id=upload_id,
            chunk_index=chunk_index,
            multipart_upload_id=upload.multipart_upload_id,
            size=len(body),
            sha256=chunk_fingerprint,
        )


def write_to_storage(
    upload_id: str, chunk_index: int, data: bytes, multipart_upload_id: str | None, sha256_hex: str | None = None
) -> StorageWriteResult:
    start = time.perf_counter()
    result = storage.write_chunk(
        upload_id, chunk_index, data, multipart_upload_id=multipart_upload_id, sha256_hex=sha256_hex
    )
    s3_put_latency_seconds.observe(time.perf_counter() - start)
    return result


def persist_chunk(plan: ChunkPlan, body: bytes) -> StorageWriteResult:
    """Storage write only; no DB session is open here."""
    return write_to_storage(plan.upload_id, plan.chunk_index, body, plan.multipart_upload_id, sha256_hex=plan.sha256)


CHUNK_UPSERT_COLUMNS = ("size_bytes", "chunk_checksum_sha256", "s3_key", "s3_etag", "status", "retry_count", "updated_at")


def upsert_chunk(db: Session, values: dict) -> None:
    """Insert or update the chunk row in one statement, so racing writers of the same chunk cannot collide."""
    statement = dialect_insert(db)(Chunk).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[Chunk.upload_id, Chunk.chunk_index],
        set_={column: statement.excluded[column] for column in CHUNK_UPSERT_COLUMNS},
    )
    db.execute(statement)


def _record_idempotency_key(db: Session, plan: ChunkPlan, idempotency_key: str) -> None:
    """Bind the key to this payload. If a racing request bound it to a different payload, reject (409)."""
    statement = dialect_insert(db)(ChunkRequestIdempotency).values(
        upload_id=plan.upload_id,
        chunk_index=plan.chunk_index,
        idempotency_key=idempotency_key,
        request_fingerprint=plan.sha256,
    )
    db.execute(
        statement.on_conflict_do_nothing(
            index_elements=[
                ChunkRequestIdempotency.upload_id,
                ChunkRequestIdempotency.chunk_index,
                ChunkRequestIdempotency.idempotency_key,
            ]
        )
    )
    stored = db.scalar(
        select(ChunkRequestIdempotency.request_fingerprint).where(
            ChunkRequestIdempotency.upload_id == plan.upload_id,
            ChunkRequestIdempotency.chunk_index == plan.chunk_index,
            ChunkRequestIdempotency.idempotency_key == idempotency_key,
        )
    )
    if stored != plan.sha256:
        raise HTTPException(status_code=409, detail="idempotency key reused with different chunk payload")


def record_chunk(plan: ChunkPlan, result: StorageWriteResult, retries: int, idempotency_key: str | None) -> None:
    """Record the stored chunk in one short transaction.

    The upload row is locked (SELECT ... FOR UPDATE on Postgres) and its status rechecked, so a
    chunk cannot be recorded after the upload completed while this request was writing to storage.
    Any error rolls back the whole transaction, including the chunk row.
    """
    db_t0 = time.perf_counter()
    with SessionLocal() as db:
        upload = db.scalar(select(Upload).where(Upload.id == plan.upload_id).with_for_update())
        if upload is None:
            raise HTTPException(status_code=404, detail="upload not found")
        if upload.status not in (UploadStatus.in_progress.value, UploadStatus.initiated.value):
            raise HTTPException(status_code=409, detail="upload is not accepting chunks")
        upsert_chunk(
            db,
            {
                "upload_id": plan.upload_id,
                "chunk_index": plan.chunk_index,
                "size_bytes": plan.size,
                "chunk_checksum_sha256": plan.sha256,
                "s3_key": result.key,
                "s3_etag": result.etag,
                "status": ChunkStatus.uploaded.value,
                "retry_count": retries,
                "updated_at": utc_now(),
            },
        )
        if upload.status == UploadStatus.initiated.value:
            upload.status = UploadStatus.in_progress.value
        if idempotency_key:
            _record_idempotency_key(db, plan, idempotency_key)
        db.commit()
    db_update_latency_seconds.observe(time.perf_counter() - db_t0)
    chunks_uploaded_total.inc()
    bytes_uploaded_total.inc(plan.size)
