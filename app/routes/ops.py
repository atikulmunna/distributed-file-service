"""Operational endpoints: health, version, metrics, admin maintenance."""

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import text

from app.auth import AuthUser, require_admin_user, secret_matches
from app.config import settings
from app.db import engine
from app.jobs import job_queue
from app.logging_utils import INSTANCE_ID
from app.maintenance import cleanup_once
from app.metrics import metrics_response
from app.routes import COMMON_ERROR_RESPONSES
from app.schemas import ErrorResponse, ValidationErrorResponse
from app.storage import storage
from app.storage_policy import storage_breaker

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _check(probe) -> str:
    try:
        result = probe()
    except Exception as exc:
        return f"error: {type(exc).__name__}"
    return "error: ping failed" if result is False else "ok"


def _database_ping() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


@router.get("/ready")
def ready(response: Response) -> dict:
    """Readiness: can this instance serve requests now? Checks the database, object storage (skipped
    while the storage circuit breaker is open), and the job queue. /health stays a liveness check."""
    checks = {
        "database": _check(_database_ping),
        "storage": _check(storage.check) if storage_breaker.allows_traffic() else "down: circuit breaker open",
        "queue": _check(job_queue.ping),
    }
    ok = all(value == "ok" for value in checks.values())
    response.status_code = 200 if ok else 503
    return {"status": "ready" if ok else "not ready", "instance_id": INSTANCE_ID, "checks": checks}


@router.get("/version")
def version() -> dict[str, str]:
    about = {"app_name": settings.app_name, "app_version": settings.app_version}
    if settings.app_env == "production":
        return about  # backends are deployment details; keep them off a public endpoint
    return {**about, "queue_backend": settings.queue_backend, "storage_backend": settings.storage_backend}


def require_metrics_token(authorization: str | None = Header(default=None, alias="Authorization")) -> None:
    """Open when metrics_token is empty; otherwise "Authorization: Bearer <metrics_token>" is required."""
    expected = settings.metrics_token
    if not expected:
        return
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme != "Bearer" or not secret_matches(presented.strip(), expected):
        raise HTTPException(status_code=401, detail="metrics token required", headers={"WWW-Authenticate": "Bearer"})


@router.get(
    "/metrics",
    dependencies=[Depends(require_metrics_token)],
    responses={
        401: {"model": ErrorResponse, "description": "Metrics token required (only when METRICS_TOKEN is set)"},
        422: {"model": ValidationErrorResponse, "description": "Request validation failed"},
    },
)
def metrics() -> Response:
    return metrics_response()


@router.post(
    "/v1/admin/cleanup",
    responses={**COMMON_ERROR_RESPONSES},
)
def run_cleanup(
    user: AuthUser = Depends(require_admin_user),
) -> dict:
    stats = cleanup_once()
    return {"status": "ok", "requested_by": user.user_id, **stats}
