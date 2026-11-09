"""Job queue delivery semantics (memory backend): at-least-once with leases."""

import threading
import time

from app.jobs import FinalizeJob, MemoryJobQueue, Reservation


def test_enqueue_reserve_ack() -> None:
    queue = MemoryJobQueue(lease_seconds=60)
    queue.enqueue(FinalizeJob("u1"))
    reservation = queue.reserve(timeout_s=0.1)
    assert reservation is not None and reservation.job == FinalizeJob("u1")
    queue.ack(reservation)
    assert queue.reserve(timeout_s=0.05) is None


def test_reserve_times_out_when_empty() -> None:
    queue = MemoryJobQueue(lease_seconds=60)
    started = time.perf_counter()
    assert queue.reserve(timeout_s=0.1) is None
    assert time.perf_counter() - started >= 0.09


def test_reserve_wakes_up_when_a_job_arrives() -> None:
    queue = MemoryJobQueue(lease_seconds=60)
    threading.Timer(0.05, lambda: queue.enqueue(FinalizeJob("late"))).start()
    reservation = queue.reserve(timeout_s=2)
    assert reservation is not None and reservation.job.upload_id == "late"


def test_unacked_job_is_redelivered() -> None:
    queue = MemoryJobQueue(lease_seconds=0.1)
    queue.enqueue(FinalizeJob("u1"))
    first = queue.reserve(timeout_s=0.1)
    assert first is not None
    assert queue.reserve(timeout_s=0.02) is None  # leased to the first worker
    time.sleep(0.12)  # that worker died: the lease expires
    second = queue.reserve(timeout_s=0.1)
    assert second is not None and second.job.upload_id == "u1"
    assert second.token != first.token
    queue.ack(second)
    queue.ack(first)  # a late ack from the dead worker is harmless
    assert queue.reserve(timeout_s=0.05) is None


def test_release_redelivers_after_the_delay() -> None:
    queue = MemoryJobQueue(lease_seconds=60)
    queue.enqueue(FinalizeJob("u1"))
    reservation = queue.reserve(timeout_s=0.1)
    queue.release(reservation, delay_s=0.1, job=FinalizeJob("u1", attempt=1))
    assert queue.reserve(timeout_s=0.02) is None
    again = queue.reserve(timeout_s=0.5)
    assert again is not None and again.job == FinalizeJob("u1", attempt=1)


def test_ping_and_clear() -> None:
    queue = MemoryJobQueue(lease_seconds=60)
    assert queue.ping() is True
    queue.enqueue(FinalizeJob("u1"))
    queue.clear()
    assert queue.reserve(timeout_s=0.02) is None


def test_job_round_trips_through_json() -> None:
    job = FinalizeJob("u1", attempt=3)
    assert FinalizeJob.from_json(job.to_json()) == job


def test_sqs_queue_calls(monkeypatch) -> None:
    """SQS backend against a fake client (real SQS needs an AWS account; see the module docstring of app/jobs.py)."""
    import sys
    import types

    from app.jobs import SQSJobQueue

    calls: list[tuple[str, dict]] = []

    class FakeSQS:
        def __getattr__(self, name):
            def record(**kwargs):
                calls.append((name, kwargs))
                if name == "receive_message":
                    return {"Messages": [{"Body": FinalizeJob("u1").to_json(), "ReceiptHandle": "rh-1"}]}
                return {}

            return record

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: FakeSQS()))
    queue = SQSJobQueue("https://sqs.example/q", "us-east-1", lease_seconds=300)
    queue.enqueue(FinalizeJob("u1"))
    reservation = queue.reserve(timeout_s=30)
    queue.release(reservation, delay_s=5)  # same job: hide it for 5 s
    queue.release(reservation, delay_s=8, job=FinalizeJob("u1", attempt=1))  # new attempt: new message
    queue.ack(reservation)

    assert reservation == Reservation(FinalizeJob("u1"), "rh-1")
    names = [name for name, _ in calls]
    assert names == ["send_message", "receive_message", "change_message_visibility", "send_message", "delete_message", "delete_message"]
    receive = calls[1][1]
    assert receive["VisibilityTimeout"] == 300 and receive["WaitTimeSeconds"] == 20
    assert calls[2][1]["VisibilityTimeout"] == 5
    assert calls[3][1]["DelaySeconds"] == 8 and FinalizeJob.from_json(calls[3][1]["MessageBody"]).attempt == 1
