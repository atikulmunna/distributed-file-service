import math
import time
import asyncio
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from collections.abc import Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from opentelemetry import trace

from app.auth import AuthUser, require_admin_user, require_api_user
from app.config import settings
from app.db import get_db
from app.durable_queue import ChunkResultStore, ChunkWriteTask, build_durable_queue
from app.limits import PerUploadInflightLimiter
from app.maintenance import cleanup_once
from app.metrics import (
    bytes_uploaded_total,
    chunks_uploaded_total,
    chunk_upload_failures_total,
    db_update_latency_seconds,
    http_request_duration_seconds,
    metrics_response,
    retries_total,
    s3_put_latency_seconds,
)
from app.models import (
    Chunk,
    ChunkRequestIdempotency,
    ChunkStatus,
    CompleteRequestIdempotency,
    InitRequestIdempotency,
    Upload,
    UploadStatus,
)
from app.schemas import (
    CompleteUploadResponse,
    ErrorResponse,
    InitUploadRequest,
    InitUploadResponse,
    MissingChunksResponse,
    UploadChunkResponse,
)
from app.db import SessionLocal
from app.storage import storage
from app.tracing import setup_tracing
from app.ui import ui_html
from app.worker import executor

durable_queue = build_durable_queue()
chunk_result_store = ChunkResultStore()


def _use_external_durable_queue() -> bool:
    return settings.queue_backend.lower() in ("redis", "sqs")


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task] = []

    async def _periodic_cleanup_loop() -> None:
        while not stop_event.is_set():
            try:
                with SessionLocal() as db:
                    cleanup_once(db)
            except Exception as exc:
                _log_event({"event": "cleanup_error", "detail": str(exc), "error_class": "maintenance_error"})
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(1, settings.cleanup_interval_seconds))
            except asyncio.TimeoutError:
                pass

    async def _autoscale_workers_loop() -> None:
        while not stop_event.is_set():
            try:
                queued, inflight, current = executor.snapshot()
                utilization = inflight / max(1, current)
                desired = current
                if (
                    queued >= settings.scale_up_queue_threshold
                    and utilization >= settings.scale_up_utilization_threshold
                    and current < settings.max_workers
                ):
                    desired = current + 1
                elif queued == 0 and utilization <= settings.scale_down_utilization_threshold and current > settings.min_workers:
                    desired = current - 1

                if desired != current:
                    executor.resize(desired)
                    _log_event(
                        {
                            "event": "worker_pool_scaled",
                            "from_workers": current,
                            "to_workers": desired,
                            "queued": queued,
                            "inflight": inflight,
                            "utilization": round(utilization, 3),
                        }
                    )
            except Exception as exc:
                _log_event({"event": "autoscale_error", "detail": str(exc), "error_class": "maintenance_error"})
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(1, settings.autoscale_cooldown_seconds))
            except asyncio.TimeoutError:
                pass

    def _process_queue_message(message) -> None:
        try:
            key, etag = _persist_chunk(
                upload_id=message.task.upload_id,
                chunk_index=message.task.chunk_index,
                data=message.task.data(),
                multipart_upload_id=message.task.multipart_upload_id,
            )
            chunk_result_store.set_success(message.task.task_id, key=key, etag=etag)
            durable_queue.ack(message.receipt)
        except Exception as exc:
            chunk_result_store.set_error(message.task.task_id, error=str(exc))
            try:
                durable_queue.ack(message.receipt)
            except Exception:
                pass

    async def _durable_queue_consumer_loop(consumer_id: int) -> None:
        while not stop_event.is_set():
            try:
                message = await asyncio.to_thread(durable_queue.dequeue, settings.queue_poll_timeout_seconds)
                if message is None:
                    continue
                await asyncio.to_thread(_process_queue_message, message)
            except Exception as exc:
                _log_event(
                    {
                        "event": "queue_consumer_error",
                        "consumer_id": consumer_id,
                        "detail": str(exc),
                        "error_class": "queue_error",
                    }
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass

    if settings.cleanup_enabled:
        tasks.append(asyncio.create_task(_periodic_cleanup_loop()))
    if settings.autoscale_enabled:
        executor.resize(max(settings.min_workers, min(settings.worker_count, settings.max_workers)))
        tasks.append(asyncio.create_task(_autoscale_workers_loop()))
    if _use_external_durable_queue():
        for consumer_id in range(max(1, settings.queue_consumer_count)):
            tasks.append(asyncio.create_task(_durable_queue_consumer_loop(consumer_id)))
    yield
    stop_event.set()
    for task in tasks:
        await task


app = FastAPI(title=settings.app_name, lifespan=lifespan)
setup_tracing(app)
_fair_share_cap = (
    settings.max_fair_inflight_chunks_per_upload
    if settings.max_fair_inflight_chunks_per_upload > 0
    else max(1, settings.worker_count // 2)
)
upload_limiter = PerUploadInflightLimiter(settings.max_inflight_chunks_per_upload, fair_share_limit=_fair_share_cap)
request_logger = logging.getLogger("dfs.request")
if not request_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    request_logger.addHandler(handler)
request_logger.setLevel(logging.INFO)
audit_logger = logging.getLogger("dfs.audit")
if not audit_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(handler)
audit_logger.setLevel(logging.INFO)
MIN_MULTIPART_PART_SIZE = 5 * 1024 * 1024


def _fingerprint(obj: dict) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _upload_id(request: Request) -> str | None:
    return request.path_params.get("upload_id")


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return str(route.path)
    return request.url.path


def _log_event(payload: dict) -> None:
    payload.setdefault("trace_id", _trace_id())
    request_logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _audit_event(payload: dict) -> None:
    payload.setdefault("trace_id", _trace_id())
    audit_logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _trace_id() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    if not span_context or not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x")


def _error_code_for_status(status_code: int) -> str:
    mapping = {
        400: "bad_request",
        401: "missing_api_key",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        416: "range_not_satisfiable",
        429: "throttled",
        500: "internal_error",
    }
    return mapping.get(status_code, f"http_{status_code}")


COMMON_ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Missing API key"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    429: {"model": ErrorResponse, "description": "Throttled request"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


def _get_owned_upload(db: Session, upload_id: str, user: AuthUser) -> Upload:
    upload = db.get(Upload, upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="upload not found")
    if upload.owner_id != user.user_id:
        raise HTTPException(status_code=403, detail="forbidden for this upload owner")
    return upload


@app.middleware("http")
async def request_context_and_logging(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    start = time.perf_counter()

    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    duration_seconds = duration_ms / 1000.0
    response.headers["X-Request-ID"] = request_id
    response.headers["X-DFS-App-Version"] = settings.app_version
    http_request_duration_seconds.labels(
        method=request.method,
        route=_route_label(request),
        status_code=str(response.status_code),
    ).observe(duration_seconds)

    _log_event(
        {
            "event": "request_completed",
            "request_id": request_id,
            "upload_id": _upload_id(request),
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        }
    )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    _log_event(
        {
            "event": "request_error",
            "request_id": _request_id(request),
            "upload_id": _upload_id(request),
            "method": request.method,
            "path": request.url.path,
            "status_code": exc.status_code,
            "error_class": "client_error" if 400 <= exc.status_code < 500 else "server_error",
            "detail": str(exc.detail),
        }
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": str(exc.detail),
            "error_code": _error_code_for_status(exc.status_code),
            "request_id": _request_id(request),
            "upload_id": _upload_id(request),
            "trace_id": _trace_id(),
        },
        headers=exc.headers or {},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    _log_event(
        {
            "event": "request_error",
            "request_id": _request_id(request),
            "upload_id": _upload_id(request),
            "method": request.method,
            "path": request.url.path,
            "status_code": 500,
            "error_class": "unhandled_exception",
            "detail": str(exc),
        }
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "internal server error",
            "error_code": "internal_error",
            "request_id": _request_id(request),
            "upload_id": _upload_id(request),
            "trace_id": _trace_id(),
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/version")
def version() -> dict[str, str]:
    return {
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "queue_backend": settings.queue_backend,
        "storage_backend": settings.storage_backend,
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
@app.get("/console", response_class=HTMLResponse)
def web_console() -> HTMLResponse:
    return HTMLResponse(content=ui_html())


@app.get("/metrics")
def metrics() -> Response:
    return metrics_response()


@app.post(
    "/v1/admin/cleanup",
    responses={**COMMON_ERROR_RESPONSES},
)
def run_cleanup(
    user: AuthUser = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    stats = cleanup_once(db)
    return {"status": "ok", "requested_by": user.user_id, **stats}


@app.post(
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

    total_chunks = math.ceil(payload.file_size / chunk_size)
    upload = Upload(
        owner_id=user.user_id,
        file_name=payload.file_name,
        file_size=payload.file_size,
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        file_checksum_sha256=payload.file_checksum_sha256.lower() if payload.file_checksum_sha256 else None,
        status=UploadStatus.initiated.value,
    )
    db.add(upload)
    db.flush()
    use_multipart = (
        settings.storage_backend.lower() in ("s3", "r2")
        and total_chunks > 1
        and chunk_size >= MIN_MULTIPART_PART_SIZE
    )
    if use_multipart:
        try:
            upload.multipart_upload_id = storage.initialize_upload(upload.id)
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"failed to initialize upload storage: {exc}") from exc
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
    _audit_event(
        {
            "event": "audit",
            "action": "upload_init",
            "request_id": _request_id(request),
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


def _persist_chunk(
    upload_id: str, chunk_index: int, data: bytes, multipart_upload_id: str | None
) -> tuple[str, str | None]:
    start = time.perf_counter()
    result = storage.write_chunk(upload_id, chunk_index, data, multipart_upload_id=multipart_upload_id)
    s3_put_latency_seconds.observe(time.perf_counter() - start)
    return result.key, result.etag


def _persist_chunk_via_durable_queue(
    upload_id: str, chunk_index: int, data: bytes, multipart_upload_id: str | None
) -> tuple[str, str | None]:
    task = ChunkWriteTask.from_bytes(
        upload_id=upload_id,
        chunk_index=chunk_index,
        data=data,
        multipart_upload_id=multipart_upload_id,
    )
    durable_queue.enqueue(task)
    outcome = chunk_result_store.wait(task.task_id, timeout_seconds=settings.queue_task_timeout_seconds)
    if outcome is None:
        raise HTTPException(status_code=504, detail="chunk task timeout while waiting for durable queue result")
    success, payload = outcome
    if not success:
        raise RuntimeError(payload["error"])
    return payload["key"], payload["etag"]


@app.put(
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
    db: Session = Depends(get_db),
) -> UploadChunkResponse:
    upload = _get_owned_upload(db, upload_id, user)
    if upload.status not in (UploadStatus.in_progress.value, UploadStatus.initiated.value):
        raise HTTPException(status_code=409, detail="upload is not accepting chunks")
    if chunk_index < 0 or chunk_index >= upload.total_chunks:
        raise HTTPException(status_code=400, detail="chunk index out of bounds")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="chunk payload is empty")
    if content_length and content_length != len(body):
        raise HTTPException(status_code=400, detail="content-length mismatch")

    chunk_fingerprint = hashlib.sha256(body).hexdigest()
    if chunk_sha256 and chunk_sha256.lower() != chunk_fingerprint:
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
            existing_chunk = db.scalar(select(Chunk).where(Chunk.upload_id == upload_id, Chunk.chunk_index == chunk_index))
            if existing_chunk and existing_chunk.status == ChunkStatus.uploaded.value:
                return UploadChunkResponse(
                    upload_id=upload_id,
                    chunk_index=chunk_index,
                    status=existing_chunk.status,
                )

    upload_limiter.acquire(upload_id)
    try:
        retries = 0
        while True:
            try:
                if _use_external_durable_queue():
                    s3_key, s3_etag = _persist_chunk_via_durable_queue(
                        upload_id, chunk_index, body, upload.multipart_upload_id
                    )
                else:
                    future = executor.submit(
                        _persist_chunk, upload_id, chunk_index, body, upload.multipart_upload_id
                    )
                    s3_key, s3_etag = future.result()
                break
            except HTTPException:
                raise
            except Exception as exc:
                retries += 1
                retries_total.inc()
                if retries > settings.max_retries:
                    chunk_upload_failures_total.inc()
                    raise HTTPException(status_code=500, detail=f"chunk upload failed: {exc}") from exc

        db_t0 = time.perf_counter()
        existing = db.scalar(select(Chunk).where(Chunk.upload_id == upload_id, Chunk.chunk_index == chunk_index))
        if existing:
            existing.size_bytes = len(body)
            existing.chunk_checksum_sha256 = chunk_fingerprint
            existing.s3_key = s3_key
            existing.s3_etag = s3_etag
            existing.status = ChunkStatus.uploaded.value
            existing.retry_count = retries
        else:
            db.add(
                Chunk(
                    upload_id=upload_id,
                    chunk_index=chunk_index,
                    size_bytes=len(body),
                    chunk_checksum_sha256=chunk_fingerprint,
                    s3_key=s3_key,
                    s3_etag=s3_etag,
                    status=ChunkStatus.uploaded.value,
                    retry_count=retries,
                )
            )
        if upload.status == UploadStatus.initiated.value:
            upload.status = UploadStatus.in_progress.value
        if idempotency_key:
            db.add(
                ChunkRequestIdempotency(
                    upload_id=upload_id,
                    chunk_index=chunk_index,
                    idempotency_key=idempotency_key,
                    request_fingerprint=chunk_fingerprint,
                )
            )
        db.commit()
        db_update_latency_seconds.observe(time.perf_counter() - db_t0)
        chunks_uploaded_total.inc()
        bytes_uploaded_total.inc(len(body))
    finally:
        upload_limiter.release(upload_id)

    return UploadChunkResponse(upload_id=upload_id, chunk_index=chunk_index, status=ChunkStatus.uploaded.value)


@app.post(
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
    upload = _get_owned_upload(db, upload_id, user)

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
        _audit_event(
            {
                "event": "audit",
                "action": "upload_complete",
                "request_id": _request_id(request),
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
    _audit_event(
        {
            "event": "audit",
            "action": "upload_complete",
            "request_id": _request_id(request),
            "upload_id": upload.id,
            "user_id": user.user_id,
            "status": upload.status,
            "idempotent_replay": False,
        }
    )
    return CompleteUploadResponse(upload_id=upload.id, status=upload.status)


@app.get(
    "/v1/uploads/{upload_id}/missing-chunks",
    response_model=MissingChunksResponse,
    responses={**COMMON_ERROR_RESPONSES, 404: {"model": ErrorResponse, "description": "Upload not found"}},
)
def missing_chunks(
    upload_id: str,
    user: AuthUser = Depends(require_api_user),
    db: Session = Depends(get_db),
) -> MissingChunksResponse:
    upload = _get_owned_upload(db, upload_id, user)

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


@app.get(
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
    upload = _get_owned_upload(db, upload_id, user)
    if upload.status != UploadStatus.completed.value:
        raise HTTPException(status_code=409, detail="upload is not completed")

    chunks = list(db.scalars(select(Chunk).where(Chunk.upload_id == upload_id).order_by(Chunk.chunk_index)).all())
    if len(chunks) != upload.total_chunks:
        raise HTTPException(status_code=500, detail="upload metadata is inconsistent")

    headers = {"Accept-Ranges": "bytes"}
    _audit_event(
        {
            "event": "audit",
            "action": "download",
            "request_id": _request_id(request),
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
