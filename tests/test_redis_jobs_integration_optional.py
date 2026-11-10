"""Job queue semantics on a real Redis (Streams + consumer group). Enable with RUN_REDIS_INTEGRATION=1;
REDIS_URL defaults to redis://localhost:6379/0. Each test uses its own stream."""

import os
import time
import uuid

import pytest

from app.jobs import FinalizeJob, RedisStreamJobQueue

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

pytestmark = pytest.mark.skipif(os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="Set RUN_REDIS_INTEGRATION=1.")


@pytest.fixture
def stream() -> str:
    name = f"dfs:test:{uuid.uuid4().hex}"
    yield name
    RedisStreamJobQueue(REDIS_URL, name, lease_seconds=1).clear()


def test_redis_enqueue_reserve_ack(stream) -> None:
    queue = RedisStreamJobQueue(REDIS_URL, stream, lease_seconds=60)
    assert queue.ping() is True
    queue.enqueue(FinalizeJob("u1"))
    reservation = queue.reserve(timeout_s=1)
    assert reservation is not None
    assert reservation.job == FinalizeJob("u1")
    queue.ack(reservation)
    assert queue.reserve(timeout_s=0.2) is None


def test_redis_stream_redelivers_after_lease(stream) -> None:
    crashed = RedisStreamJobQueue(REDIS_URL, stream, lease_seconds=1, consumer="worker-a")
    survivor = RedisStreamJobQueue(REDIS_URL, stream, lease_seconds=1, consumer="worker-b")
    crashed.enqueue(FinalizeJob("u1"))
    first = crashed.reserve(timeout_s=1)
    assert first is not None  # worker-a takes it and dies without acking
    assert survivor.reserve(timeout_s=0.2) is None  # still leased
    time.sleep(1.2)
    second = survivor.reserve(timeout_s=1)  # XAUTOCLAIM hands it to worker-b
    assert second is not None
    assert second.job == FinalizeJob("u1")
    survivor.ack(second)
    assert survivor.reserve(timeout_s=0.2) is None


def test_redis_release_requeues_with_attempt(stream) -> None:
    queue = RedisStreamJobQueue(REDIS_URL, stream, lease_seconds=60)
    queue.enqueue(FinalizeJob("u1"))
    reservation = queue.reserve(timeout_s=1)
    queue.release(reservation, delay_s=0, job=FinalizeJob("u1", attempt=1))
    again = queue.reserve(timeout_s=1)
    assert again is not None
    assert again.job == FinalizeJob("u1", attempt=1)
    queue.ack(again)
    assert queue.reserve(timeout_s=0.2) is None
