from concurrent.futures import Future, ThreadPoolExecutor
from threading import Condition, Lock

from fastapi import HTTPException

from app.config import settings
from app.metrics import inflight_chunks, task_queue_depth, throttled_requests_total, worker_busy_count, worker_count

THROTTLE_HEADERS_BASE = {"Retry-After": "1"}


class BackpressureExecutor:
    """Fixed-size pool for storage writes with bounded admission: 429 when the queue or the global
    in-flight limit is full. The size is fixed: autoscaling was measured and did not help."""

    def __init__(self, workers: int, queue_maxsize: int, global_inflight_limit: int) -> None:
        self.workers = workers
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="storage")
        self.queue_maxsize = queue_maxsize
        self.global_inflight_limit = global_inflight_limit
        self._lock = Lock()
        self._idle = Condition(self._lock)  # notified when nothing is queued or running
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
            worker_busy_count.set(min(self._inflight, self.workers))

    def _on_end(self) -> None:
        with self._lock:
            self._inflight -= 1
            inflight_chunks.set(self._inflight)
            worker_busy_count.set(min(self._inflight, self.workers))
            if self._queued == 0 and self._inflight == 0:
                self._idle.notify_all()

    def drain(self, timeout_s: float) -> int:
        """Wait up to timeout_s for queued and running storage work to finish; return how many tasks
        are left. The pool stays usable: it is shared by every app lifespan in the process."""
        with self._idle:
            self._idle.wait_for(lambda: self._queued + self._inflight == 0, timeout=timeout_s)
            return self._queued + self._inflight

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
