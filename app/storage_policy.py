"""The single retry policy for object storage calls, a circuit breaker, and how failures map to HTTP.

Transient errors (throttling, 5xx, timeouts, connection failures) are retried with capped exponential
backoff and full jitter. If they persist, the client gets 503 with Retry-After so it backs off and
retries the request. Permanent errors fail fast with a generic 500. The underlying error is logged,
never returned, since it can name buckets, endpoints, or credentials.

The circuit breaker (per instance) counts consecutive transient failed attempts. At
breaker_failure_threshold it opens: for breaker_open_seconds every storage call fails immediately with
503 instead of tying up a worker thread on timeouts. Then a single probe call is let through
(half-open): success closes the breaker, a transient failure reopens it. A permanent error proves
storage is reachable, so it closes the breaker rather than tripping it.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import TypeVar

from fastapi import HTTPException

from app.config import settings
from app.logging_utils import log_event
from app.metrics import storage_breaker_state
from app.retry import call_with_retry
from app.storage import is_transient_storage_error

T = TypeVar("T")

STORAGE_RETRY_AFTER_SECONDS = "2"
_STATE_VALUE = {"closed": 0, "open": 1, "half_open": 2}


class BreakerOpenError(Exception):
    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"storage circuit breaker open, retry in {retry_after_s:.1f} s")
        self.retry_after_s = retry_after_s


class StorageCircuitBreaker:
    def __init__(self, failure_threshold: int, open_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._set_state("closed")
            self._failures = 0
            self._opened_at = 0.0
            self._probe_in_flight = False

    def _set_state(self, state: str) -> None:
        if getattr(self, "state", None) != state and getattr(self, "state", None) is not None:
            log_event({"event": "storage_breaker", "from": self.state, "to": state})
        self.state = state
        storage_breaker_state.set(_STATE_VALUE[state])

    def _open(self) -> None:
        self._set_state("open")
        self._opened_at = self._clock()
        self._failures = 0
        self._probe_in_flight = False

    def before_call(self) -> None:
        """Raise BreakerOpenError unless a storage call may go ahead now."""
        with self._lock:
            if self.state == "closed":
                return
            if self.state == "open":
                remaining = self._opened_at + self.open_seconds - self._clock()
                if remaining > 0:
                    raise BreakerOpenError(remaining)
                self._set_state("half_open")
                self._probe_in_flight = True  # this caller is the probe
                return
            if self._probe_in_flight:
                raise BreakerOpenError(1.0)
            self._probe_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._probe_in_flight = False
            if self.state != "closed":
                self._set_state("closed")

    def record_failure(self, transient: bool) -> None:
        with self._lock:
            if not transient:  # storage answered: it is reachable
                self._failures = 0
                self._probe_in_flight = False
                if self.state != "closed":
                    self._set_state("closed")
                return
            if self.state == "half_open":
                self._open()
                return
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._open()

    def allows_traffic(self) -> bool:
        with self._lock:
            return self.state == "closed"


storage_breaker = StorageCircuitBreaker(settings.breaker_failure_threshold, settings.breaker_open_seconds)


def with_storage_retries(
    fn: Callable[[], T], operation: str, on_retry: Callable[[Exception], None] | None = None
) -> T:
    def attempt() -> T:
        storage_breaker.before_call()
        try:
            result = fn()
        except HTTPException:
            raise
        except Exception as exc:
            storage_breaker.record_failure(transient=is_transient_storage_error(exc))
            raise
        storage_breaker.record_success()
        return result

    def report_retry(exc: Exception) -> None:
        log_event({"event": "storage_retry", "operation": operation, "error_class": type(exc).__name__, "detail": str(exc)})
        if on_retry is not None:
            on_retry(exc)

    try:
        return call_with_retry(
            attempt,
            attempts=settings.max_retries + 1,
            base_delay_s=settings.retry_base_delay_ms / 1000,
            max_delay_s=settings.retry_max_delay_ms / 1000,
            is_retryable=is_transient_storage_error,
            on_retry=report_retry,
        )
    except BreakerOpenError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"storage temporarily unavailable during {operation}, retry later",
            headers={"Retry-After": str(max(1, math.ceil(exc.retry_after_s)))},
        ) from exc
    except HTTPException:
        raise  # already an API answer
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
