"""The single retry policy for object storage calls, and how failures map to HTTP responses.

Transient errors (throttling, 5xx, timeouts, connection failures) are retried with capped exponential
backoff and full jitter. If they persist, the client gets 503 with Retry-After so it backs off and
retries the request. Permanent errors fail fast with a generic 500. The underlying error is logged,
never returned, since it can name buckets, endpoints, or credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from fastapi import HTTPException

from app.config import settings
from app.logging_utils import log_event
from app.retry import call_with_retry
from app.storage import is_transient_storage_error

T = TypeVar("T")

STORAGE_RETRY_AFTER_SECONDS = "2"


def with_storage_retries(
    fn: Callable[[], T], operation: str, on_retry: Callable[[Exception], None] | None = None
) -> T:
    def report_retry(exc: Exception) -> None:
        log_event({"event": "storage_retry", "operation": operation, "error_class": type(exc).__name__, "detail": str(exc)})
        if on_retry is not None:
            on_retry(exc)

    try:
        return call_with_retry(
            fn,
            attempts=settings.max_retries + 1,
            base_delay_s=settings.retry_base_delay_ms / 1000,
            max_delay_s=settings.retry_max_delay_ms / 1000,
            is_retryable=is_transient_storage_error,
            on_retry=report_retry,
        )
    except HTTPException:
        raise  # already an API answer (for example a durable-queue timeout)
    except Exception as exc:
        transient = is_transient_storage_error(exc)
        log_event(
            {
                "event": "storage_error",
                "operation": operation,
                "transient": transient,
                "error_class": type(exc).__name__,
                "detail": str(exc),
            }
        )
        if transient:
            raise HTTPException(
                status_code=503,
                detail=f"storage temporarily unavailable during {operation}, retry later",
                headers={"Retry-After": STORAGE_RETRY_AFTER_SECONDS},
            ) from exc
        raise HTTPException(status_code=500, detail=f"storage error during {operation}") from exc
