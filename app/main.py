import asyncio
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings, validate_production_settings
from app.finalizer import start_workers
from app.jobs import job_queue
from app.logging_utils import INSTANCE_ID, current_trace_id, log_event, request_id_of
from app.maintenance import cleanup_once
from app.metrics import http_request_duration_seconds
from app.routes import console, ops, uploads
from app.routes.uploads import (
    upload_limiter,  # noqa: F401  (re-exported: tests and tools import it from app.main)
)
from app.tracing import setup_tracing
from app.worker import executor


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.app_env == "production":
        problems = validate_production_settings(settings)
        if problems:
            log_event({"event": "startup_refused", "app_env": settings.app_env, "problems": problems})
            raise RuntimeError("refusing to start in production: " + "; ".join(problems))
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task] = []

    async def _periodic_cleanup_loop() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(cleanup_once)  # DB and storage calls block; keep them off the loop
            except Exception as exc:
                log_event({"event": "cleanup_error", "detail": str(exc), "error_class": "maintenance_error"})
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(1, settings.cleanup_interval_seconds))
            except asyncio.TimeoutError:
                pass

    if settings.cleanup_enabled:
        tasks.append(asyncio.create_task(_periodic_cleanup_loop()))
    worker_stop = threading.Event()
    workers = []
    if settings.finalizer_in_process:
        workers = start_workers(job_queue, settings.finalizer_concurrency, worker_stop, poll_timeout_s=0.5)
    yield
    stop_event.set()
    worker_stop.set()
    for task in tasks:
        await task
    for worker in workers:
        await asyncio.to_thread(worker.join, 5)
    remaining = await asyncio.to_thread(executor.drain, settings.shutdown_drain_seconds)
    log_event({"event": "shutdown_drained", "storage_tasks_left": remaining})


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
    return "unmatched"  # never the raw path: arbitrary URLs would each create a new time series


def _error_code_for_status(status_code: int) -> str:
    mapping = {
        400: "bad_request",
        401: "missing_api_key",
        403: "forbidden",
        404: "not_found",
        408: "request_timeout",
        409: "conflict",
        411: "length_required",
        416: "range_not_satisfiable",
        422: "validation_error",
        429: "throttled",
        499: "client_closed_request",
        500: "internal_error",
        503: "unavailable",
    }
    return mapping.get(status_code, f"http_{status_code}")


def _error_body(request: Request, status_code: int, detail: str) -> dict:
    if status_code >= 500:
        # Server errors say only what kind of failure happened; the specifics are logged under request_id.
        return {
            "detail": "temporarily unavailable, retry later" if status_code == 503 else "internal error",
            "error_code": _error_code_for_status(status_code),
            "request_id": request_id_of(request),
        }
    return {
        "detail": detail,
        "error_code": _error_code_for_status(status_code),
        "request_id": request_id_of(request),
        "upload_id": _upload_id(request),
        "trace_id": current_trace_id(),
    }


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
    response.headers["X-DFS-Instance"] = INSTANCE_ID
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
        content=_error_body(request, exc.status_code, str(exc.detail)),
        headers=exc.headers or {},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Location, message and type only: the submitted value ("input") is not echoed back.
    errors = [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))} for e in exc.errors()]
    log_event(
        {
            "event": "request_error",
            "request_id": request_id_of(request),
            "upload_id": _upload_id(request),
            "method": request.method,
            "path": request.url.path,
            "status_code": 422,
            "error_class": "client_error",
            "detail": "request validation failed",
            "errors": errors,
        }
    )
    return JSONResponse(status_code=422, content={**_error_body(request, 422, "request validation failed"), "errors": errors})


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
            "exception_type": type(exc).__name__,
            "detail": str(exc),
            "traceback": "".join(traceback.format_exception(exc)),
        }
    )
    # This handler runs outside the request middleware, so it sets the correlation headers itself.
    return JSONResponse(
        status_code=500,
        content=_error_body(request, 500, "internal error"),
        headers={"X-Request-ID": request_id_of(request), "X-DFS-Instance": INSTANCE_ID},
    )
