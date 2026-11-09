"""The storage worker pool: bounded admission (429 when full) without private ThreadPoolExecutor internals."""

import re
from pathlib import Path

from fastapi import HTTPException

from app.worker import BackpressureExecutor

WORKER_SOURCE = Path(__file__).resolve().parents[1] / "app" / "worker.py"


def test_worker_pool_uses_no_private_executor_attributes() -> None:
    source = WORKER_SOURCE.read_text(encoding="utf-8")
    private = re.findall(r"\.(_max_workers|_adjust_thread_count|_threads|_work_queue)\b", source)
    assert private == [], f"app/worker.py relies on private ThreadPoolExecutor internals: {sorted(set(private))}"


def test_pool_runs_submitted_work() -> None:
    pool = BackpressureExecutor(workers=2, queue_maxsize=4, global_inflight_limit=4)
    assert pool.submit(lambda: 21 * 2).result(timeout=5) == 42


def test_pool_rejects_when_the_queue_is_full() -> None:
    pool = BackpressureExecutor(workers=1, queue_maxsize=0, global_inflight_limit=4)
    try:
        pool.submit(lambda: None)
    except HTTPException as exc:
        assert exc.status_code == 429
        assert exc.headers["X-RateLimit-Reason"] == "queue_full"
    else:
        raise AssertionError("a full queue must reject with 429")
