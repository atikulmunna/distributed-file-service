from app.worker import BackpressureExecutor


def test_resize_updates_worker_count_in_snapshot() -> None:
    executor = BackpressureExecutor(workers=2, queue_maxsize=10, global_inflight_limit=10)
    queued, inflight, workers = executor.snapshot()
    assert queued == 0
    assert inflight == 0
    assert workers == 2

    executor.resize(4)
    _, _, resized_workers = executor.snapshot()
    assert resized_workers == 4


def test_resize_ignores_non_positive_values() -> None:
    executor = BackpressureExecutor(workers=3, queue_maxsize=10, global_inflight_limit=10)
    executor.resize(0)
    _, _, workers = executor.snapshot()
    assert workers == 3
