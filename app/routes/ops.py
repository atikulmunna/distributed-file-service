"""Operational endpoints: health, version, metrics, admin maintenance."""

from fastapi import APIRouter, Depends, Response

from app.auth import AuthUser, require_admin_user
from app.config import settings
from app.maintenance import cleanup_once
from app.metrics import metrics_response
from app.routes import COMMON_ERROR_RESPONSES

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/version")
def version() -> dict[str, str]:
    return {
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "queue_backend": settings.queue_backend,
        "storage_backend": settings.storage_backend,
    }


@router.get("/metrics")
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
