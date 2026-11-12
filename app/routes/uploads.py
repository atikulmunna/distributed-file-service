"""Upload endpoints: init, chunk upload, complete, abort, the read views, download and download links."""

import asyncio
import base64
import binascii
import hashlib
import json
import uuid
from datetime import datetime
from functools import partial

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app import signed_urls
from app.auth import AuthUser, enforce_rate_limit, require_api_user
from app.chunk_service import (
    admit_chunk,
    persist_chunk,
    prepare_chunk,
    record_chunk,
)
from app.config import settings
from app.db import SessionLocal, get_db
from app.download import DownloadPlan, content_disposition, ensure_downloadable, iter_pieces, prepare_download
from app.layout import ChunkLayout
from app.limits import PerUploadInflightLimiter
from app.logging_utils import audit_event, log_event, request_id_of
from app.metrics import (
    chunk_upload_failures_total,
    retries_total,
)
from app.models import (
    CHECKSUM_MISMATCH,
    Chunk,
    ChunkStatus,
    InitRequestIdempotency,
    Upload,
    UploadStatus,
)
from app.routes import COMMON_ERROR_RESPONSES
from app.schemas import (
    ChunkInfo,
    ChunkListResponse,
    CompleteUploadResponse,
    DownloadUrlResponse,
    ErrorResponse,
    InitUploadRequest,
    InitUploadResponse,
    LimitsResponse,
    MissingChunksResponse,
    UploadChunkResponse,
    UploadDetail,
    UploadListResponse,
    UploadStatusResponse,
    UploadSummary,
)
from app.storage import storage
from app.storage_policy import with_storage_retries
from app.upload_access import get_owned_upload, scoped_idempotency_key
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


# Chunk keys are stored as sent, in a 128-character column; longer keys are rejected with 422.
MAX_IDEMPOTENCY_KEY_LENGTH = 128


def _fingerprint(obj: dict) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _abort_unused_multipart(upload_id: str, multipart_upload_id: str | None) -> None:
    """Best effort: the bucket rule for incomplete multipart uploads removes anything left behind."""
    if not multipart_upload_id:
        return
    try:
        storage.abort_upload(upload_id, multipart_upload_id)
    except Exception as exc:  # noqa: BLE001  logged; an unfinished multipart upload is cleaned up later
        log_event({"event": "init_race_abort_error", "upload_id": upload_id, "detail": str(exc)})


def _replay_init(db: Session, stored_key: str, request_fingerprint: str) -> InitUploadResponse | None:
    """The upload an earlier init with this (user-scoped) key created, or None. A different payload under
    the same key is a 409."""
    existing_request = db.get(InitRequestIdempotency, stored_key)
    if existing_request is None:
        return None
    if existing_request.request_fingerprint != request_fingerprint:
        raise HTTPException(status_code=409, detail="idempotency key reused with different init payload")
    existing_upload = db.get(Upload, existing_request.upload_id)
    if existing_upload is None:
        return None
    return InitUploadResponse(
        upload_id=existing_upload.id,
        chunk_size=existing_upload.chunk_size,
        total_chunks=existing_upload.total_chunks,
        status=existing_upload.status,
    )


@router.post(
    "/v1/uploads/init",
    response_model=InitUploadResponse,
    status_code=201,
    responses={**COMMON_ERROR_RESPONSES, 409: {"model": ErrorResponse, "description": "Idempotency conflict"}},
)
def init_upload(
    request: Request,
    payload: InitUploadRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=MAX_IDEMPOTENCY_KEY_LENGTH),
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
    stored_key = scoped_idempotency_key(user, idempotency_key) if idempotency_key else None
    if stored_key:
        replay = _replay_init(db, stored_key, request_fingerprint)
        if replay is not None:
            return replay

    # End the read transaction so no DB connection is held during the storage call below.
    db.rollback()

    total_chunks = layout.total_chunks
    upload_id = str(uuid.uuid4())
    multipart_upload_id = None
    use_multipart = (
        settings.storage_backend.lower() in ("s3", "r2") and total_chunks > 1 and chunk_size >= MIN_MULTIPART_PART_SIZE
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
    if stored_key:
        db.add(
            InitRequestIdempotency(
                idempotency_key=stored_key,
                upload_id=upload_id,
                request_fingerprint=request_fingerprint,
            )
        )
    try:
        db.commit()
    except IntegrityError:
        # A concurrent init with the same key committed between our lookup and this commit. Our upload
        # row is rolled back with the transaction; answer as a replay would.
        db.rollback()
        if not stored_key:
            raise
        _abort_unused_multipart(upload_id, multipart_upload_id)
        replay = _replay_init(db, stored_key, request_fingerprint)
        if replay is None:
            raise
        return replay
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
                raise HTTPException(
                    status_code=400, detail=f"chunk body is longer than its Content-Length of {size} bytes"
                )

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
        400: {
            "model": ErrorResponse,
            "description": "Validation error, including a size that does not match the layout",
        },
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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=MAX_IDEMPOTENCY_KEY_LENGTH),
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
        202: {
            "model": CompleteUploadResponse,
            "description": "Accepted: whole-file checksum verification in progress (VERIFYING)",
        },
        404: {"model": ErrorResponse, "description": "Upload not found"},
        409: {"model": ErrorResponse, "description": "State/idempotency conflict"},
    },
)
def complete_upload(
    request: Request,
    response: Response,
    upload_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=MAX_IDEMPOTENCY_KEY_LENGTH),
    user: AuthUser = Depends(require_api_user),
) -> CompleteUploadResponse:
    fingerprint = _fingerprint({"upload_id": upload_id})
    stored_key = scoped_idempotency_key(user, idempotency_key) if idempotency_key else None
    claimed = claim_completion(upload_id, user, stored_key, fingerprint)
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
        if claimed.response.status == UploadStatus.verifying.value:
            response.status_code = 202
        return claimed.response

    try:
        run_storage_work(claimed)
    except Exception:
        release_claim(upload_id)  # storage did not finish: let the client fix the problem and retry
        raise
    status = finish_completion(upload_id, stored_key, fingerprint, verify=bool(claimed.file_checksum_sha256))
    if status == UploadStatus.verifying.value:
        response.status_code = 202  # accepted: the whole-file checksum is being verified
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


@router.get(
    "/v1/limits",
    response_model=LimitsResponse,
    responses={**COMMON_ERROR_RESPONSES},
)
def limits(user: AuthUser = Depends(require_api_user)) -> LimitsResponse:
    """The input bounds init enforces, so a client can choose a valid chunk size up front."""
    requires_min_part = settings.storage_backend.lower() in ("s3", "r2")
    return LimitsResponse(
        max_file_size_bytes=settings.max_file_size_bytes,
        min_chunk_size_bytes=settings.min_chunk_size_bytes,
        max_chunk_size_bytes=settings.max_chunk_size_bytes,
        default_chunk_size_bytes=settings.chunk_size_bytes,
        max_total_chunks=settings.max_total_chunks,
        max_inflight_chunks_per_upload=settings.max_inflight_chunks_per_upload,
        storage_backend_requires_min_part=requires_min_part,
        min_part_size_bytes=MIN_MULTIPART_PART_SIZE if requires_min_part else None,
    )


def _encode_cursor(created_at: datetime, upload_id: str) -> str:
    raw = json.dumps([created_at.isoformat(), upload_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        created_at, upload_id = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return datetime.fromisoformat(created_at), str(upload_id)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


@router.get(
    "/v1/uploads",
    response_model=UploadListResponse,
    responses={**COMMON_ERROR_RESPONSES, 400: {"model": ErrorResponse, "description": "Invalid cursor"}},
)
def list_uploads(
    status: UploadStatus | None = Query(default=None, description="Only uploads in this status"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, description="next_cursor from the previous page"),
    user: AuthUser = Depends(require_api_user),
) -> UploadListResponse:
    """The caller's uploads, newest first. Keyset pagination: pages stay consistent while uploads are added."""
    query = select(Upload).where(Upload.owner_id == user.user_id)
    if status is not None:
        query = query.where(Upload.status == status.value)
    if cursor:
        after_created, after_id = _decode_cursor(cursor)
        query = query.where(
            or_(Upload.created_at < after_created, and_(Upload.created_at == after_created, Upload.id < after_id))
        )
    query = query.order_by(Upload.created_at.desc(), Upload.id.desc()).limit(limit + 1)
    with SessionLocal() as db:
        rows = list(db.scalars(query))
        page = rows[:limit]
        counts = db.execute(
            select(Chunk.upload_id, func.count())
            .where(Chunk.upload_id.in_([row.id for row in page]), Chunk.status == ChunkStatus.uploaded.value)
            .group_by(Chunk.upload_id)
        ).all()
        uploaded = {str(upload_id): int(count) for upload_id, count in counts}
        items = [
            UploadSummary(
                upload_id=row.id,
                file_name=row.file_name,
                file_size=row.file_size,
                total_chunks=row.total_chunks,
                uploaded_chunks=uploaded.get(row.id, 0),
                status=row.status,
                created_at=row.created_at,
                last_activity_at=row.last_activity_at,
            )
            for row in page
        ]
        next_cursor = _encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
    return UploadListResponse(items=items, next_cursor=next_cursor)


def _checksum_verified(upload: Upload) -> bool | None:
    if not upload.file_checksum_sha256:
        return None  # nothing to verify
    if upload.status == UploadStatus.completed.value:
        return True  # with a declared checksum, COMPLETED is reached only through a matching verification
    if upload.status == UploadStatus.failed.value and (upload.failure_reason or "").startswith(CHECKSUM_MISMATCH):
        return False
    return None  # not verified yet


@router.get(
    "/v1/uploads/{upload_id}",
    response_model=UploadDetail,
    responses={**COMMON_ERROR_RESPONSES, 404: {"model": ErrorResponse, "description": "Upload not found"}},
)
def upload_detail(upload_id: str, user: AuthUser = Depends(require_api_user)) -> UploadDetail:
    """Everything about one upload. Poll it after a 202 VERIFYING from complete."""
    with SessionLocal() as db:
        upload = get_owned_upload(db, upload_id, user)
        uploaded_chunks, uploaded_bytes = db.execute(
            select(func.count(Chunk.id), func.coalesce(func.sum(Chunk.size_bytes), 0)).where(
                Chunk.upload_id == upload_id, Chunk.status == ChunkStatus.uploaded.value
            )
        ).one()
        # Built inside the session; it closes before the response is validated and sent.
        return UploadDetail(
            upload_id=upload.id,
            file_name=upload.file_name,
            file_size=upload.file_size,
            chunk_size=upload.chunk_size,
            total_chunks=upload.total_chunks,
            status=upload.status,
            uploaded_chunks=uploaded_chunks,
            uploaded_bytes=uploaded_bytes,
            file_checksum_sha256=upload.file_checksum_sha256,
            checksum_verified=_checksum_verified(upload),
            failure_reason=upload.failure_reason,
            created_at=upload.created_at,
            updated_at=upload.updated_at,
            last_activity_at=upload.last_activity_at,
        )


@router.get(
    "/v1/uploads/{upload_id}/chunks",
    response_model=ChunkListResponse,
    responses={**COMMON_ERROR_RESPONSES, 404: {"model": ErrorResponse, "description": "Upload not found"}},
)
def list_chunks(
    upload_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
    user: AuthUser = Depends(require_api_user),
) -> ChunkListResponse:
    """Recorded chunks in index order. Indexes missing from the list have not been received."""
    with SessionLocal() as db:
        get_owned_upload(db, upload_id, user)
        total = db.scalar(select(func.count()).select_from(Chunk).where(Chunk.upload_id == upload_id)) or 0
        rows = db.scalars(
            select(Chunk).where(Chunk.upload_id == upload_id).order_by(Chunk.chunk_index).offset(offset).limit(limit)
        )
        items = [
            ChunkInfo(
                index=row.chunk_index,
                size_bytes=row.size_bytes,
                sha256=row.chunk_checksum_sha256,
                status=row.status,
                retry_count=row.retry_count,
                updated_at=row.updated_at,
            )
            for row in rows
        ]
    return ChunkListResponse(items=items, total=total)


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
    return _stream_download(request, plan, user, via="api", range_requested=bool(range))


def _stream_download(request: Request, plan: DownloadPlan, user: AuthUser, via: str, range_requested: bool) -> Response:
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition(plan.file_name),
        "Content-Length": str(plan.length),
        "Cache-Control": "private, no-store",  # user data: never kept by a shared cache
    }
    status_code = 200
    if plan.byte_range:
        status_code = 206
        headers["Content-Range"] = f"bytes {plan.byte_range.start}-{plan.byte_range.end}/{plan.file_size}"
    audit_event(
        {
            "event": "audit",
            "action": "download",
            "via": via,
            "request_id": request_id_of(request),
            "upload_id": plan.upload_id,
            "user_id": user.user_id,
            "status": UploadStatus.completed.value,
            "range_requested": range_requested,
        }
    )
    return StreamingResponse(
        iter_pieces(plan.pieces, settings.download_segment_bytes, settings.download_prefetch),
        status_code=status_code,
        media_type="application/octet-stream",
        headers=headers,
    )


@router.post(
    "/v1/uploads/{upload_id}/download-url",
    response_model=DownloadUrlResponse,
    responses={
        **COMMON_ERROR_RESPONSES,
        404: {"model": ErrorResponse, "description": "Upload not found"},
        409: {"model": ErrorResponse, "description": "Upload not completed"},
    },
)
def create_download_url(
    request: Request, upload_id: str, user: AuthUser = Depends(require_api_user)
) -> DownloadUrlResponse:
    """A link that downloads this upload without an API key until it expires (download_url_ttl_seconds)."""
    with SessionLocal() as db:
        ensure_downloadable(get_owned_upload(db, upload_id, user))
    token, expires = signed_urls.issue(upload_id, user.user_id)
    audit_event(
        {
            "event": "audit",
            "action": "download_url_issued",
            "request_id": request_id_of(request),
            "upload_id": upload_id,
            "user_id": user.user_id,
            "expires": expires,
        }
    )
    return DownloadUrlResponse(
        url=str(request.url_for("signed_download", token=token)), expires_at=signed_urls.expiry_datetime(expires)
    )


@router.get(
    "/v1/downloads/{token}",
    name="signed_download",
    responses={
        403: {"model": ErrorResponse, "description": "Invalid or expired download link"},
        404: {"model": ErrorResponse, "description": "Upload not found"},
        409: {"model": ErrorResponse, "description": "Upload not completed"},
        416: {"model": ErrorResponse, "description": "Invalid range request"},
        429: {"model": ErrorResponse, "description": "The link owner's rate limit is exhausted"},
    },
)
def signed_download(request: Request, token: str, range: str | None = Header(default=None)) -> Response:
    """Download by signed link (from POST /v1/uploads/{upload_id}/download-url); supports Range."""
    try:
        grant = signed_urls.verify(token)
    except signed_urls.InvalidDownloadLink:
        raise HTTPException(status_code=403, detail="invalid or expired download link") from None
    enforce_rate_limit(grant.owner_id)  # downloads by link spend the owner's budget
    owner = AuthUser(user_id=grant.owner_id, api_key="download_link")
    plan = prepare_download(grant.upload_id, owner, range)
    return _stream_download(request, plan, owner, via="download_link", range_requested=bool(range))
