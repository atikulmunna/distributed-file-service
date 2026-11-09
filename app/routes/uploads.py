"""Upload lifecycle endpoints: init, chunk upload, complete, missing chunks, download."""

import asyncio
import hashlib
import json
import uuid
from functools import partial

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.auth import AuthUser, require_api_user
from app.chunk_service import (
    admit_chunk,
    persist_chunk,
    persist_chunk_via_queue,
    prepare_chunk,
    record_chunk,
    use_external_durable_queue,
)
from app.config import settings
from app.db import get_db
from app.download import content_disposition, iter_pieces, prepare_download
from app.layout import ChunkLayout
from app.limits import PerUploadInflightLimiter
from app.logging_utils import audit_event, request_id_of
from app.metrics import (
    chunk_upload_failures_total,
    retries_total,
)
from app.models import (
    Chunk,
    ChunkStatus,
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
    UploadStatusResponse,
)
from app.storage import storage
from app.storage_policy import with_storage_retries
from app.upload_access import get_owned_upload
from app.upload_lifecycle import (
    CompletionReplay,
    begin_abort,
    claim_completion,
    discard_storage,
    finish_completion,
    release_claim,
    run_storage_work,
)
from app.worker import executor

router = APIRouter()

_fair_share_cap = (
    settings.max_fair_inflight_chunks_per_upload
    if settings.max_fair_inflight_chunks_per_upload > 0
    else max(1, settings.worker_count // 2)
)
upload_limiter = PerUploadInflightLimiter(settings.max_inflight_chunks_per_upload, fair_share_limit=_fair_share_cap)

MIN_MULTIPART_PART_SIZE = 5 * 1024 * 1024


S3_SMALL_PART_REASON = (
    "chunk_size must be at least 5 MiB (5242880 bytes) when an upload has more than one chunk on the s3 backend"
)


def _validated_layout(file_size: int, chunk_size: int) -> ChunkLayout:
    """Apply the configured input bounds. Raises 400 with the reason a client can act on."""
    if file_size > settings.max_file_size_bytes:
        raise HTTPException(
            status_code=400, detail=f"file_size {file_size} exceeds the maximum of {settings.max_file_size_bytes} bytes"
        )
    if not settings.min_chunk_size_bytes <= chunk_size <= settings.max_chunk_size_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"chunk_size must be between {settings.min_chunk_size_bytes} and {settings.max_chunk_size_bytes} bytes",
        )
    layout = ChunkLayout(file_size, chunk_size)
    if layout.total_chunks > settings.max_total_chunks:
        raise HTTPException(
            status_code=400,
            detail=f"file needs {layout.total_chunks} chunks; the maximum is {settings.max_total_chunks}, use a larger chunk_size",
        )
    if (
        settings.storage_backend.lower() in ("s3", "r2")
        and layout.total_chunks > 1
        and chunk_size < MIN_MULTIPART_PART_SIZE
    ):
        raise HTTPException(status_code=400, detail=S3_SMALL_PART_REASON)
    return layout


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
    layout = _validated_layout(payload.file_size, chunk_size)
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

    total_chunks = layout.total_chunks
    upload_id = str(uuid.uuid4())
    multipart_upload_id = None
    use_multipart = (
        settings.storage_backend.lower() in ("s3", "r2")
        and total_chunks > 1
        and chunk_size >= MIN_MULTIPART_PART_SIZE
    )
    if use_multipart:
        multipart_upload_id = with_storage_retries(lambda: storage.initialize_upload(upload_id), "upload init")

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
                upload_id=upload_id,
                request_fingerprint=request_fingerprint,
            )
        )
    db.commit()
    # Do not touch the ORM object after commit: reloading it would open a new transaction and
    # hold the connection until the response is validated, which needs a free worker thread.
    status = UploadStatus.initiated.value
    audit_event(
        {
            "event": "audit",
            "action": "upload_init",
            "request_id": request_id_of(request),
            "upload_id": upload_id,
            "user_id": user.user_id,
            "status": status,
            "file_size": payload.file_size,
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
        }
    )

    return InitUploadResponse(
        upload_id=upload_id,
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        status=status,
    )


async def _read_chunk_body(request: Request, size: int, timeout_s: float) -> bytes:
    """Read exactly `size` bytes within `timeout_s`. Nothing is stored and no slot is held while reading."""
    pieces: list[bytes] = []
    received = 0

    async def read_all() -> None:
        nonlocal received
        async for piece in request.stream():
            pieces.append(piece)
            received += len(piece)
            if received > size:
                raise HTTPException(status_code=400, detail=f"chunk body is longer than its Content-Length of {size} bytes")

    try:
        await asyncio.wait_for(read_all(), timeout=timeout_s)
    except TimeoutError as exc:
        raise HTTPException(status_code=408, detail=f"chunk body not received within {timeout_s} s") from exc
    except ClientDisconnect as exc:
        raise HTTPException(status_code=499, detail="client disconnected before sending the whole chunk") from exc
    if received != size:
        raise HTTPException(
            status_code=400, detail=f"chunk body is {received} bytes, shorter than its Content-Length of {size}"
        )
    return b"".join(pieces)  # one copy; cheaper on the event loop than growing a bytearray


@router.put(
    "/v1/uploads/{upload_id}/chunks/{chunk_index}",
    response_model=UploadChunkResponse,
    status_code=202,
    responses={
        **COMMON_ERROR_RESPONSES,
        400: {"model": ErrorResponse, "description": "Validation error, including a size that does not match the layout"},
        404: {"model": ErrorResponse, "description": "Upload not found"},
        408: {"model": ErrorResponse, "description": "Chunk body not received in time"},
        409: {"model": ErrorResponse, "description": "State/idempotency conflict"},
        411: {"model": ErrorResponse, "description": "Content-Length header missing"},
    },
)
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request,
    content_length: int | None = Header(default=None),
    chunk_sha256: str | None = Header(default=None, alias="X-Chunk-SHA256"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthUser = Depends(require_api_user),
) -> UploadChunkResponse:
    # Only awaits happen on the event loop. Validation (with hashing), the storage write, and
    # the DB write each run on worker threads, and no DB connection is held between them.
    if content_length is None:
        raise HTTPException(status_code=411, detail="Content-Length header is required")
    expected_size = await run_in_threadpool(admit_chunk, upload_id, chunk_index, user)
    if content_length != expected_size:
        raise HTTPException(
            status_code=400,
            detail=f"chunk {chunk_index} has Content-Length {content_length}, expected {expected_size} bytes",
        )
    body = await _read_chunk_body(request, expected_size, settings.chunk_body_timeout_seconds)
    prepared = await run_in_threadpool(
        prepare_chunk, upload_id, chunk_index, body, chunk_sha256, idempotency_key, user, declared_length=content_length
    )
    if isinstance(prepared, UploadChunkResponse):
        return prepared
    plan = prepared

    upload_limiter.acquire(upload_id)
    try:
        retried: list[Exception] = []

        def count_retry(exc: Exception) -> None:
            retried.append(exc)
            retries_total.inc()

        # Retries (transient storage errors only, with backoff) run inside the storage worker thread.
        if use_external_durable_queue():
            write = partial(with_storage_retries, partial(persist_chunk_via_queue, plan, body), "chunk write", count_retry)
            pending = run_in_threadpool(write)
        else:
            write = partial(with_storage_retries, partial(persist_chunk, plan, body), "chunk write", count_retry)
            pending = asyncio.wrap_future(executor.submit(write))
        try:
            result = await pending
        except HTTPException as exc:
            if exc.status_code >= 500:
                chunk_upload_failures_total.inc()
            raise

        await run_in_threadpool(record_chunk, plan, result, len(retried), idempotency_key)
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
) -> CompleteUploadResponse:
    fingerprint = _fingerprint({"upload_id": upload_id})
    claimed = claim_completion(upload_id, user, idempotency_key, fingerprint)
    if isinstance(claimed, CompletionReplay):
        if claimed.already_completed:
            audit_event(
                {
                    "event": "audit",
                    "action": "upload_complete",
                    "request_id": request_id_of(request),
                    "upload_id": upload_id,
                    "user_id": user.user_id,
                    "status": claimed.response.status,
                    "idempotent_replay": True,
                }
            )
        return claimed.response

    try:
        run_storage_work(claimed)
    except Exception:
        release_claim(upload_id)  # storage did not finish: let the client fix the problem and retry
        raise
    status = finish_completion(upload_id, idempotency_key, fingerprint)
    audit_event(
        {
            "event": "audit",
            "action": "upload_complete",
            "request_id": request_id_of(request),
            "upload_id": upload_id,
            "user_id": user.user_id,
            "status": status,
            "idempotent_replay": False,
        }
    )
    return CompleteUploadResponse(upload_id=upload_id, status=status)


@router.delete(
    "/v1/uploads/{upload_id}",
    response_model=UploadStatusResponse,
    responses={
        **COMMON_ERROR_RESPONSES,
        404: {"model": ErrorResponse, "description": "Upload not found"},
        409: {"model": ErrorResponse, "description": "Upload is completing or completed"},
    },
)
def abort_upload_endpoint(
    request: Request,
    upload_id: str,
    user: AuthUser = Depends(require_api_user),
) -> UploadStatusResponse:
    """Abort an unfinished upload: mark it ABORTED and discard its stored data. Repeating it is a no-op."""
    work, failure_reason = begin_abort(upload_id, user)
    if work is not None:
        discard_storage(work)
        audit_event(
            {
                "event": "audit",
                "action": "upload_abort",
                "request_id": request_id_of(request),
                "upload_id": upload_id,
                "user_id": user.user_id,
                "status": UploadStatus.aborted.value,
            }
        )
    return UploadStatusResponse(upload_id=upload_id, status=UploadStatus.aborted.value, failure_reason=failure_reason)


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
) -> Response:
    # Everything needed is read in one short session; no DB connection is held while streaming.
    plan = prepare_download(upload_id, user, range)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition(plan.file_name),
        "Content-Length": str(plan.length),
    }
    status_code = 200
    if plan.byte_range:
        status_code = 206
        headers["Content-Range"] = f"bytes {plan.byte_range.start}-{plan.byte_range.end}/{plan.file_size}"
    audit_event(
        {
            "event": "audit",
            "action": "download",
            "request_id": request_id_of(request),
            "upload_id": upload_id,
            "user_id": user.user_id,
            "status": UploadStatus.completed.value,
            "range_requested": bool(range),
        }
    )
    return StreamingResponse(
        iter_pieces(plan.pieces, settings.download_segment_bytes, settings.download_prefetch),
        status_code=status_code,
        media_type="application/octet-stream",
        headers=headers,
    )
