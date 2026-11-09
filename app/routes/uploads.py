"""Upload lifecycle endpoints: init, chunk upload, complete, missing chunks, download."""

import asyncio
import hashlib
import json
import math
import uuid
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import AuthUser, require_api_user
from app.chunk_service import (
    persist_chunk,
    persist_chunk_via_queue,
    prepare_chunk,
    record_chunk,
    use_external_durable_queue,
)
from app.config import settings
from app.db import get_db
from app.limits import PerUploadInflightLimiter
from app.logging_utils import audit_event, request_id_of
from app.metrics import (
    chunk_upload_failures_total,
    retries_total,
)
from app.models import (
    Chunk,
    ChunkStatus,
    CompleteRequestIdempotency,
    InitRequestIdempotency,
    Upload,
    UploadStatus,
)
from app.routes import COMMON_ERROR_RESPONSES
from app.schemas import (
    CompleteUploadResponse,
    ErrorResponse,
    InitUploadRequest,
    InitUploadResponse,
    MissingChunksResponse,
    UploadChunkResponse,
)
from app.storage import storage
from app.upload_access import get_owned_upload
from app.worker import executor

router = APIRouter()

_fair_share_cap = (
    settings.max_fair_inflight_chunks_per_upload
    if settings.max_fair_inflight_chunks_per_upload > 0
    else max(1, settings.worker_count // 2)
)
upload_limiter = PerUploadInflightLimiter(settings.max_inflight_chunks_per_upload, fair_share_limit=_fair_share_cap)

MIN_MULTIPART_PART_SIZE = 5 * 1024 * 1024


def _fingerprint(obj: dict) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@router.post(
    "/v1/uploads/init",
    response_model=InitUploadResponse,
    status_code=201,
    responses={**COMMON_ERROR_RESPONSES, 409: {"model": ErrorResponse, "description": "Idempotency conflict"}},
)
def init_upload(
    request: Request,
    payload: InitUploadRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthUser = Depends(require_api_user),
    db: Session = Depends(get_db),
) -> InitUploadResponse:
    chunk_size = payload.chunk_size or settings.chunk_size_bytes
    request_fingerprint = _fingerprint(
        {
            "file_name": payload.file_name,
            "file_size": payload.file_size,
            "chunk_size": chunk_size,
            "file_checksum_sha256": payload.file_checksum_sha256.lower() if payload.file_checksum_sha256 else None,
        }
    )
    if idempotency_key:
        existing_request = db.get(InitRequestIdempotency, idempotency_key)
        if existing_request:
            if existing_request.request_fingerprint != request_fingerprint:
                raise HTTPException(status_code=409, detail="idempotency key reused with different init payload")
            existing_upload = db.get(Upload, existing_request.upload_id)
            if existing_upload:
                if existing_upload.owner_id != user.user_id:
                    raise HTTPException(status_code=403, detail="idempotency key belongs to a different owner")
                return InitUploadResponse(
                    upload_id=existing_upload.id,
                    chunk_size=existing_upload.chunk_size,
                    total_chunks=existing_upload.total_chunks,
                    status=existing_upload.status,
                )

    # End the read transaction so no DB connection is held during the storage call below.
    db.rollback()

    total_chunks = math.ceil(payload.file_size / chunk_size)
    upload_id = str(uuid.uuid4())
    multipart_upload_id = None
    use_multipart = (
        settings.storage_backend.lower() in ("s3", "r2")
        and total_chunks > 1
        and chunk_size >= MIN_MULTIPART_PART_SIZE
    )
    if use_multipart:
        try:
            multipart_upload_id = storage.initialize_upload(upload_id)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to initialize upload storage: {exc}") from exc

    upload = Upload(
        id=upload_id,
        owner_id=user.user_id,
        file_name=payload.file_name,
        file_size=payload.file_size,
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        file_checksum_sha256=payload.file_checksum_sha256.lower() if payload.file_checksum_sha256 else None,
        status=UploadStatus.initiated.value,
        multipart_upload_id=multipart_upload_id,
    )
    db.add(upload)
    db.flush()
    if idempotency_key:
        db.add(
            InitRequestIdempotency(
                idempotency_key=idempotency_key,
                upload_id=upload.id,
                request_fingerprint=request_fingerprint,
            )
        )
    db.commit()
    db.refresh(upload)
    audit_event(
        {
            "event": "audit",
            "action": "upload_init",
            "request_id": request_id_of(request),
            "upload_id": upload.id,
            "user_id": user.user_id,
            "status": upload.status,
            "file_size": upload.file_size,
            "chunk_size": upload.chunk_size,
            "total_chunks": upload.total_chunks,
        }
    )

    return InitUploadResponse(
        upload_id=upload.id,
        chunk_size=upload.chunk_size,
        total_chunks=upload.total_chunks,
        status=upload.status,
    )


@router.put(
    "/v1/uploads/{upload_id}/chunks/{chunk_index}",
    response_model=UploadChunkResponse,
    status_code=202,
    responses={
        **COMMON_ERROR_RESPONSES,
        400: {"model": ErrorResponse, "description": "Validation error"},
        404: {"model": ErrorResponse, "description": "Upload not found"},
        409: {"model": ErrorResponse, "description": "State/idempotency conflict"},
    },
)
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request,
    content_length: int = Header(default=0),
    chunk_sha256: str | None = Header(default=None, alias="X-Chunk-SHA256"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthUser = Depends(require_api_user),
) -> UploadChunkResponse:
    # Only awaits happen on the event loop. Validation (with hashing), the storage write, and
    # the DB write each run on worker threads, and no DB connection is held between them.
    body = await request.body()
    prepared = await run_in_threadpool(
        prepare_chunk, upload_id, chunk_index, body, chunk_sha256, idempotency_key, user, declared_length=content_length
    )
    if isinstance(prepared, UploadChunkResponse):
        return prepared
    plan = prepared

    upload_limiter.acquire(upload_id)
    try:
        retries = 0
        while True:
            try:
                if use_external_durable_queue():
                    result = await run_in_threadpool(persist_chunk_via_queue, plan, body)
                else:
                    result = await asyncio.wrap_future(executor.submit(persist_chunk, plan, body))
                break
            except HTTPException:
                raise
            except Exception as exc:
                retries += 1
                retries_total.inc()
                if retries > settings.max_retries:
                    chunk_upload_failures_total.inc()
                    raise HTTPException(status_code=500, detail=f"chunk upload failed: {exc}") from exc

        await run_in_threadpool(record_chunk, plan, result, retries, idempotency_key)
    finally:
        upload_limiter.release(upload_id)

    return UploadChunkResponse(upload_id=upload_id, chunk_index=chunk_index, status=ChunkStatus.uploaded.value)


@router.post(
    "/v1/uploads/{upload_id}/complete",
    response_model=CompleteUploadResponse,
    responses={
        **COMMON_ERROR_RESPONSES,
        404: {"model": ErrorResponse, "description": "Upload not found"},
        409: {"model": ErrorResponse, "description": "State/idempotency conflict"},
    },
)
def complete_upload(
    request: Request,
    upload_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthUser = Depends(require_api_user),
    db: Session = Depends(get_db),
) -> CompleteUploadResponse:
    request_fingerprint = _fingerprint({"upload_id": upload_id})
    upload = get_owned_upload(db, upload_id, user)

    if idempotency_key:
        existing_request = db.get(CompleteRequestIdempotency, idempotency_key)
        if existing_request:
            if existing_request.request_fingerprint != request_fingerprint:
                raise HTTPException(status_code=409, detail="idempotency key reused with different complete payload")
            if existing_request.upload_id == upload_id:
                existing_upload = db.get(Upload, upload_id)
                if existing_upload:
                    if existing_upload.owner_id != user.user_id:
                        raise HTTPException(status_code=403, detail="idempotency key belongs to a different owner")
                    return CompleteUploadResponse(upload_id=existing_upload.id, status=existing_upload.status)

    if upload.status == UploadStatus.initiated.value:
        raise HTTPException(status_code=409, detail="cannot complete upload from INITIATED state")
    if upload.status == UploadStatus.completed.value:
        if idempotency_key:
            existing_request = db.get(CompleteRequestIdempotency, idempotency_key)
            if not existing_request:
                db.add(
                    CompleteRequestIdempotency(
                        idempotency_key=idempotency_key,
                        upload_id=upload_id,
                        request_fingerprint=request_fingerprint,
                    )
                )
                db.commit()
        audit_event(
            {
                "event": "audit",
                "action": "upload_complete",
                "request_id": request_id_of(request),
                "upload_id": upload.id,
                "user_id": user.user_id,
                "status": upload.status,
                "idempotent_replay": True,
            }
        )
        return CompleteUploadResponse(upload_id=upload.id, status=upload.status)
    if upload.status != UploadStatus.in_progress.value:
        raise HTTPException(status_code=409, detail="cannot complete upload from current state")

    uploaded_count = db.scalar(
        select(func.count(Chunk.id)).where(Chunk.upload_id == upload_id, Chunk.status == ChunkStatus.uploaded.value)
    )
    if uploaded_count != upload.total_chunks:
        raise HTTPException(status_code=409, detail="cannot complete upload, missing chunks")

    uploaded_chunks = list(db.scalars(select(Chunk).where(Chunk.upload_id == upload_id).order_by(Chunk.chunk_index)).all())
    if upload.file_checksum_sha256:
        full_file_hash = hashlib.sha256()
        for chunk in uploaded_chunks:
            full_file_hash.update(storage.read_chunk(chunk.s3_key))
        if full_file_hash.hexdigest() != upload.file_checksum_sha256:
            raise HTTPException(status_code=409, detail="file checksum mismatch")
    if settings.storage_backend.lower() in ("s3", "r2") and upload.multipart_upload_id:
        parts: list[dict] = []
        for chunk in uploaded_chunks:
            if not chunk.s3_etag:
                raise HTTPException(status_code=409, detail="cannot complete upload, missing S3 part etag")
            parts.append({"PartNumber": chunk.chunk_index + 1, "ETag": chunk.s3_etag})
        try:
            storage.complete_upload(upload_id=upload_id, multipart_upload_id=upload.multipart_upload_id, parts=parts)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to complete multipart upload: {exc}") from exc

    upload.status = UploadStatus.completed.value
    if idempotency_key:
        db.add(
            CompleteRequestIdempotency(
                idempotency_key=idempotency_key,
                upload_id=upload_id,
                request_fingerprint=request_fingerprint,
            )
        )
    db.commit()
    audit_event(
        {
            "event": "audit",
            "action": "upload_complete",
            "request_id": request_id_of(request),
            "upload_id": upload.id,
            "user_id": user.user_id,
            "status": upload.status,
            "idempotent_replay": False,
        }
    )
    return CompleteUploadResponse(upload_id=upload.id, status=upload.status)


@router.get(
    "/v1/uploads/{upload_id}/missing-chunks",
    response_model=MissingChunksResponse,
    responses={**COMMON_ERROR_RESPONSES, 404: {"model": ErrorResponse, "description": "Upload not found"}},
)
def missing_chunks(
    upload_id: str,
    user: AuthUser = Depends(require_api_user),
    db: Session = Depends(get_db),
) -> MissingChunksResponse:
    upload = get_owned_upload(db, upload_id, user)

    uploaded_indexes = set(
        db.scalars(
            select(Chunk.chunk_index).where(Chunk.upload_id == upload_id, Chunk.status == ChunkStatus.uploaded.value)
        ).all()
    )
    missing = [idx for idx in range(upload.total_chunks) if idx not in uploaded_indexes]
    return MissingChunksResponse(upload_id=upload_id, missing_chunk_indexes=missing, status=upload.status)


def _parse_range(range_header: str, file_size: int) -> tuple[int, int]:
    if not range_header.startswith("bytes="):
        raise HTTPException(status_code=416, detail="invalid range header")
    parts = range_header.removeprefix("bytes=").split("-", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=416, detail="invalid range format")

    start = int(parts[0]) if parts[0] else 0
    end = int(parts[1]) if parts[1] else file_size - 1
    if start < 0 or end < start or end >= file_size:
        raise HTTPException(status_code=416, detail="range out of bounds")
    return start, end


def _stream_bytes_for_range(chunks: list[Chunk], start: int, end: int) -> Iterator[bytes]:
    cursor = 0
    for chunk in chunks:
        data = storage.read_chunk(chunk.s3_key)
        next_cursor = cursor + len(data)

        if next_cursor <= start:
            cursor = next_cursor
            continue
        if cursor > end:
            break

        read_start = max(0, start - cursor)
        read_end = min(len(data) - 1, end - cursor)
        if read_start <= read_end:
            yield data[read_start : read_end + 1]
        cursor = next_cursor


@router.get(
    "/v1/uploads/{upload_id}/download",
    responses={
        **COMMON_ERROR_RESPONSES,
        404: {"model": ErrorResponse, "description": "Upload not found"},
        409: {"model": ErrorResponse, "description": "Upload not completed"},
        416: {"model": ErrorResponse, "description": "Invalid range request"},
    },
)
def download(
    request: Request,
    upload_id: str,
    range: str | None = Header(default=None),
    user: AuthUser = Depends(require_api_user),
    db: Session = Depends(get_db),
) -> Response:
    upload = get_owned_upload(db, upload_id, user)
    if upload.status != UploadStatus.completed.value:
        raise HTTPException(status_code=409, detail="upload is not completed")

    chunks = list(db.scalars(select(Chunk).where(Chunk.upload_id == upload_id).order_by(Chunk.chunk_index)).all())
    if len(chunks) != upload.total_chunks:
        raise HTTPException(status_code=500, detail="upload metadata is inconsistent")

    safe_name = upload.file_name.replace('"', "")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{safe_name}"',
    }
    audit_event(
        {
            "event": "audit",
            "action": "download",
            "request_id": request_id_of(request),
            "upload_id": upload.id,
            "user_id": user.user_id,
            "status": upload.status,
            "range_requested": bool(range),
        }
    )
    if range:
        start, end = _parse_range(range, upload.file_size)
        headers["Content-Range"] = f"bytes {start}-{end}/{upload.file_size}"
        return StreamingResponse(
            _stream_bytes_for_range(chunks, start, end),
            status_code=206,
            media_type="application/octet-stream",
            headers=headers,
        )

    return StreamingResponse(
        _stream_bytes_for_range(chunks, 0, upload.file_size - 1),
        media_type="application/octet-stream",
        headers=headers,
    )
