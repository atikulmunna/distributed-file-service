import asyncio
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import SessionLocal
from app.logging_utils import current_trace_id, log_event, request_id_of
from app.maintenance import cleanup_once
from app.metrics import http_request_duration_seconds
from app.chunk_service import chunk_result_store, durable_queue, use_external_durable_queue, write_to_storage
from app.routes import console, ops, uploads
from app.routes.uploads import upload_limiter  # noqa: F401  (re-exported: tests and tools import it from app.main)
from app.tracing import setup_tracing
from app.worker import executor


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
                log_event({"event": "cleanup_error", "detail": str(exc), "error_class": "maintenance_error"})
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
                    log_event(
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
                log_event({"event": "autoscale_error", "detail": str(exc), "error_class": "maintenance_error"})
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(1, settings.autoscale_cooldown_seconds))
            except asyncio.TimeoutError:
                pass

    def _process_queue_message(message) -> None:
        try:
            result = write_to_storage(
                upload_id=message.task.upload_id,
                chunk_index=message.task.chunk_index,
                data=message.task.data(),
                multipart_upload_id=message.task.multipart_upload_id,
            )
            chunk_result_store.set_success(message.task.task_id, key=result.key, etag=result.etag)
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
                log_event(
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
    if use_external_durable_queue():
        for consumer_id in range(max(1, settings.queue_consumer_count)):
            tasks.append(asyncio.create_task(_durable_queue_consumer_loop(consumer_id)))
    yield
    stop_event.set()
    for task in tasks:
        await task


app = FastAPI(title=settings.app_name, lifespan=lifespan)
setup_tracing(app)

app.include_router(ops.router)
app.include_router(console.router)
app.include_router(uploads.router)


def _upload_id(request: Request) -> str | None:
    return request.path_params.get("upload_id")


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return str(route.path)
    return request.url.path


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

    log_event(
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
    log_event(
        {
            "event": "request_error",
            "request_id": request_id_of(request),
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
            "request_id": request_id_of(request),
            "upload_id": _upload_id(request),
            "trace_id": current_trace_id(),
        },
        headers=exc.headers or {},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log_event(
        {
            "event": "request_error",
            "request_id": request_id_of(request),
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
            "request_id": request_id_of(request),
            "upload_id": _upload_id(request),
            "trace_id": current_trace_id(),
        },
    )
