from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock

from fastapi import HTTPException

from app.config import settings
from app.metrics import inflight_chunks, task_queue_depth, throttled_requests_total, worker_busy_count, worker_count

THROTTLE_HEADERS_BASE = {"Retry-After": "1"}


class BackpressureExecutor:
    def __init__(self, workers: int, queue_maxsize: int, global_inflight_limit: int) -> None:
        self.executor = ThreadPoolExecutor(max_workers=workers)
        self.queue_maxsize = queue_maxsize
        self.global_inflight_limit = global_inflight_limit
        self._lock = Lock()
        self._queued = 0
        self._inflight = 0
        worker_count.set(workers)

    def _try_admit(self) -> None:
        with self._lock:
            if self._queued >= self.queue_maxsize:
                throttled_requests_total.inc()
                raise HTTPException(
                    status_code=429,
                    detail="task queue is full",
                    headers={**THROTTLE_HEADERS_BASE, "X-RateLimit-Reason": "queue_full"},
                )
            if self._inflight >= self.global_inflight_limit:
                throttled_requests_total.inc()
                raise HTTPException(
                    status_code=429,
                    detail="global inflight chunk limit reached",
                    headers={**THROTTLE_HEADERS_BASE, "X-RateLimit-Reason": "global_inflight_limit"},
                )
            self._queued += 1
            task_queue_depth.set(self._queued)

    def _on_start(self) -> None:
        with self._lock:
            self._queued -= 1
            self._inflight += 1
            task_queue_depth.set(self._queued)
            inflight_chunks.set(self._inflight)
            worker_busy_count.set(min(self._inflight, self.executor._max_workers))

    def _on_end(self) -> None:
        with self._lock:
            self._inflight -= 1
            inflight_chunks.set(self._inflight)
            worker_busy_count.set(min(self._inflight, self.executor._max_workers))

    def submit(self, fn, *args, **kwargs) -> Future:
        self._try_admit()

        def wrapped():
            self._on_start()
            try:
                return fn(*args, **kwargs)
            finally:
                self._on_end()

        return self.executor.submit(wrapped)


executor = BackpressureExecutor(
    workers=settings.worker_count,
    queue_maxsize=settings.task_queue_maxsize,
    global_inflight_limit=settings.max_global_inflight_chunks,
)
