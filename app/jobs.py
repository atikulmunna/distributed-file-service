"""Durable job queue for post-completion work (whole-file checksum verification).

Delivery is at least once: a reserved job is leased to one worker; if the worker does not ack it
before the lease expires (it crashed), the job is delivered again. Jobs must therefore be idempotent.
Chunk bytes never travel through this queue; jobs only carry an upload ID.

Backends:
  memory  in-process (tests, single-process dev). Leases expire and are redelivered.
  redis   Redis Streams with a consumer group: XADD, XREADGROUP, XACK, and XAUTOCLAIM to take over
          entries whose lease (idle time) expired.
  sqs     SQS: the visibility timeout is the lease; delete on ack.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, cast

from app.config import settings


@dataclass(frozen=True)
class FinalizeJob:
    upload_id: str
    attempt: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> FinalizeJob:
        return cls(**json.loads(payload))


@dataclass(frozen=True)
class Reservation:
    job: FinalizeJob
    token: str  # backend handle used to ack or release this delivery


class JobQueue:
    def enqueue(self, job: FinalizeJob) -> None:
        raise NotImplementedError

    def reserve(self, timeout_s: float) -> Reservation | None:
        """Lease the next job, waiting up to timeout_s. None when there is none."""
        raise NotImplementedError

    def ack(self, reservation: Reservation) -> None:
        """The job is done; it will not be delivered again."""
        raise NotImplementedError

    def release(self, reservation: Reservation, delay_s: float = 0, job: FinalizeJob | None = None) -> None:
        """Give the job back for another attempt after delay_s, optionally as an updated job."""
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError

    def clear(self) -> None:
        """Drop every job (tests only)."""
        raise NotImplementedError


class MemoryJobQueue(JobQueue):
    def __init__(self, lease_seconds: float) -> None:
        self._lease = lease_seconds
        self._cond = threading.Condition()
        self._ready: list[tuple[float, FinalizeJob]] = []  # (available at, job)
        self._leased: dict[str, tuple[float, FinalizeJob]] = {}  # token -> (lease deadline, job)

    def enqueue(self, job: FinalizeJob) -> None:
        with self._cond:
            self._ready.append((time.monotonic(), job))
            self._cond.notify()

    def reserve(self, timeout_s: float) -> Reservation | None:
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                now = time.monotonic()
                for token, (lease_deadline, job) in list(self._leased.items()):
                    if lease_deadline <= now:  # the holder died: deliver again
                        del self._leased[token]
                        self._ready.append((now, job))
                available = [item for item in self._ready if item[0] <= now]
                if available:
                    item = min(available, key=lambda entry: entry[0])
                    self._ready.remove(item)
                    token = uuid.uuid4().hex
                    self._leased[token] = (now + self._lease, item[1])
                    return Reservation(item[1], token)
                remaining = deadline - now
                if remaining <= 0:
                    return None
                upcoming = [at for at, _ in self._ready] + [lease for lease, _ in self._leased.values()]
                self._cond.wait(min([remaining] + [max(0.001, at - now) for at in upcoming]))

    def ack(self, reservation: Reservation) -> None:
        with self._cond:
            self._leased.pop(reservation.token, None)

    def release(self, reservation: Reservation, delay_s: float = 0, job: FinalizeJob | None = None) -> None:
        with self._cond:
            if self._leased.pop(reservation.token, None) is None:
                return  # the lease already expired and the job was delivered again
            self._ready.append((time.monotonic() + delay_s, job or reservation.job))
            self._cond.notify()

    def ping(self) -> bool:
        return True

    def clear(self) -> None:
        with self._cond:
            self._ready.clear()
            self._leased.clear()


class RedisStreamJobQueue(JobQueue):
    GROUP = "finalizers"

    def __init__(self, redis_url: str, stream: str, lease_seconds: float, consumer: str | None = None) -> None:
        import redis

        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._stream = stream
        self._lease_ms = max(1, int(lease_seconds * 1000))
        self._consumer = consumer or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._group_ready = False

    def _ensure_group(self) -> None:
        if self._group_ready:
            return
        import redis

        try:
            self._redis.xgroup_create(self._stream, self.GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    def enqueue(self, job: FinalizeJob) -> None:
        self._redis.xadd(self._stream, {"job": job.to_json()})

    def reserve(self, timeout_s: float) -> Reservation | None:
        self._ensure_group()
        # First take over an entry whose holder has been idle longer than the lease.
        # cast: redis-py types its sync and asyncio clients together (results are "Awaitable | Any").
        claimed = cast(
            "list[Any]",
            self._redis.xautoclaim(
                self._stream, self.GROUP, self._consumer, min_idle_time=self._lease_ms, start_id="0-0", count=1
            ),
        )
        for entry_id, fields in claimed[1]:
            if fields:  # deleted entries come back without fields
                return Reservation(FinalizeJob.from_json(fields["job"]), entry_id)
        response = cast(
            "list[Any]",
            self._redis.xreadgroup(
                self.GROUP, self._consumer, {self._stream: ">"}, count=1, block=max(1, int(timeout_s * 1000))
            ),
        )
        if not response:
            return None
        entry_id, fields = response[0][1][0]
        return Reservation(FinalizeJob.from_json(fields["job"]), entry_id)

    def ack(self, reservation: Reservation) -> None:
        self._redis.xack(self._stream, self.GROUP, reservation.token)
        self._redis.xdel(self._stream, reservation.token)

    def release(self, reservation: Reservation, delay_s: float = 0, job: FinalizeJob | None = None) -> None:
        # Streams have no delayed delivery: wait in this worker, then re-add and ack the old entry.
        if delay_s > 0:
            time.sleep(min(delay_s, 60))
        self._redis.xadd(self._stream, {"job": (job or reservation.job).to_json()})
        self.ack(reservation)

    def ping(self) -> bool:
        return bool(self._redis.ping())

    def clear(self) -> None:
        self._redis.delete(self._stream)
        self._group_ready = False


class SQSJobQueue(JobQueue):
    def __init__(self, queue_url: str, region: str, lease_seconds: float) -> None:
        import boto3

        if not queue_url:
            raise ValueError("sqs_queue_url must be set when queue_backend=sqs")
        self._client = boto3.client("sqs", region_name=region)
        self._url = queue_url
        self._lease = max(1, min(43200, int(lease_seconds)))

    def enqueue(self, job: FinalizeJob) -> None:
        self._client.send_message(QueueUrl=self._url, MessageBody=job.to_json())

    def reserve(self, timeout_s: float) -> Reservation | None:
        response = self._client.receive_message(
            QueueUrl=self._url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=max(0, min(20, int(timeout_s))),
            VisibilityTimeout=self._lease,
        )
        messages = response.get("Messages", [])
        if not messages:
            return None
        return Reservation(FinalizeJob.from_json(messages[0]["Body"]), messages[0]["ReceiptHandle"])

    def ack(self, reservation: Reservation) -> None:
        self._client.delete_message(QueueUrl=self._url, ReceiptHandle=reservation.token)

    def release(self, reservation: Reservation, delay_s: float = 0, job: FinalizeJob | None = None) -> None:
        if job is not None and job != reservation.job:  # a new attempt count: send a new message
            self._client.send_message(
                QueueUrl=self._url, MessageBody=job.to_json(), DelaySeconds=max(0, min(900, int(delay_s)))
            )
            self.ack(reservation)
            return
        self._client.change_message_visibility(
            QueueUrl=self._url, ReceiptHandle=reservation.token, VisibilityTimeout=max(0, int(delay_s))
        )

    def ping(self) -> bool:
        self._client.get_queue_attributes(QueueUrl=self._url, AttributeNames=["QueueArn"])
        return True

    def clear(self) -> None:
        self._client.purge_queue(QueueUrl=self._url)


def build_job_queue() -> JobQueue:
    backend = settings.queue_backend.lower()
    if backend == "memory":
        return MemoryJobQueue(lease_seconds=settings.job_lease_seconds)
    if backend == "redis":
        return RedisStreamJobQueue(settings.redis_url, settings.redis_stream_name, settings.job_lease_seconds)
    if backend == "sqs":
        return SQSJobQueue(settings.sqs_queue_url, settings.aws_region, settings.job_lease_seconds)
    raise ValueError(f"unsupported queue backend: {settings.queue_backend}")


job_queue = build_job_queue()
