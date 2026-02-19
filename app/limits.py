from threading import Lock

from fastapi import HTTPException

from app.metrics import throttled_requests_total
from app.worker import THROTTLE_HEADERS_BASE


class PerUploadInflightLimiter:
    def __init__(self, limit: int, fair_share_limit: int | None = None) -> None:
        self.limit = limit
        self.fair_share_limit = fair_share_limit
        self._counts: dict[str, int] = {}
        self._lock = Lock()

    def acquire(self, upload_id: str) -> None:
        with self._lock:
            current = self._counts.get(upload_id, 0)
            if current >= self.limit:
                throttled_requests_total.inc()
                raise HTTPException(
                    status_code=429,
                    detail="per-upload inflight chunk limit reached",
                    headers={**THROTTLE_HEADERS_BASE, "X-RateLimit-Reason": "upload_inflight_limit"},
                )
            if self.fair_share_limit is not None and self.fair_share_limit > 0 and current >= self.fair_share_limit:
                throttled_requests_total.inc()
                raise HTTPException(
                    status_code=429,
                    detail="per-upload fair-share limit reached",
                    headers={**THROTTLE_HEADERS_BASE, "X-RateLimit-Reason": "upload_fair_share_limit"},
                )
            self._counts[upload_id] = current + 1

    def release(self, upload_id: str) -> None:
        with self._lock:
            current = self._counts.get(upload_id, 0)
            next_value = max(0, current - 1)
            if next_value == 0:
                self._counts.pop(upload_id, None)
            else:
                self._counts[upload_id] = next_value
