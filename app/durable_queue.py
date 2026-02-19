import base64
import json
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass

from app.config import settings


@dataclass
class ChunkWriteTask:
    task_id: str
    upload_id: str
    chunk_index: int
    multipart_upload_id: str | None
    data_b64: str

    @classmethod
    def from_bytes(
        cls, upload_id: str, chunk_index: int, data: bytes, multipart_upload_id: str | None = None
    ) -> "ChunkWriteTask":
        return cls(
            task_id=str(uuid.uuid4()),
            upload_id=upload_id,
            chunk_index=chunk_index,
            multipart_upload_id=multipart_upload_id,
            data_b64=base64.b64encode(data).decode("ascii"),
        )

    def data(self) -> bytes:
        return base64.b64decode(self.data_b64.encode("ascii"))

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> "ChunkWriteTask":
        parsed = json.loads(payload)
        return cls(**parsed)


@dataclass
class QueueMessage:
    receipt: str
    task: ChunkWriteTask


class DurableQueue:
    def enqueue(self, task: ChunkWriteTask) -> None:
        raise NotImplementedError

    def dequeue(self, timeout_seconds: int) -> QueueMessage | None:
        raise NotImplementedError

    def ack(self, receipt: str) -> None:
        raise NotImplementedError


class MemoryDurableQueue(DurableQueue):
    def __init__(self) -> None:
        self._q: queue.Queue[ChunkWriteTask] = queue.Queue()

    def enqueue(self, task: ChunkWriteTask) -> None:
        self._q.put(task)

    def dequeue(self, timeout_seconds: int) -> QueueMessage | None:
        try:
            task = self._q.get(timeout=max(0.01, timeout_seconds))
            return QueueMessage(receipt=task.task_id, task=task)
        except queue.Empty:
            return None

    def ack(self, receipt: str) -> None:
        return None


class RedisDurableQueue(DurableQueue):
    def __init__(self, redis_url: str, queue_name: str) -> None:
        import redis

        self._client = redis.Redis.from_url(redis_url, decode_responses=True)
        self._queue_name = queue_name

    def enqueue(self, task: ChunkWriteTask) -> None:
        self._client.rpush(self._queue_name, task.to_json())

    def dequeue(self, timeout_seconds: int) -> QueueMessage | None:
        result = self._client.blpop(self._queue_name, timeout=max(1, int(timeout_seconds)))
        if not result:
            return None
        _, payload = result
        task = ChunkWriteTask.from_json(payload)
        return QueueMessage(receipt=task.task_id, task=task)

    def ack(self, receipt: str) -> None:
        return None


class SQSDurableQueue(DurableQueue):
    def __init__(self, queue_url: str, region: str) -> None:
        import boto3

        if not queue_url:
            raise ValueError("sqs_queue_url must be set when queue_backend=sqs")
        self._queue_url = queue_url
        self._client = boto3.client("sqs", region_name=region)

    def enqueue(self, task: ChunkWriteTask) -> None:
        self._client.send_message(QueueUrl=self._queue_url, MessageBody=task.to_json())

    def dequeue(self, timeout_seconds: int) -> QueueMessage | None:
        response = self._client.receive_message(
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=max(1, min(20, int(timeout_seconds))),
            VisibilityTimeout=max(30, int(settings.queue_task_timeout_seconds)),
        )
        messages = response.get("Messages", [])
        if not messages:
            return None
        message = messages[0]
        task = ChunkWriteTask.from_json(message["Body"])
        return QueueMessage(receipt=message["ReceiptHandle"], task=task)

    def ack(self, receipt: str) -> None:
        self._client.delete_message(QueueUrl=self._queue_url, ReceiptHandle=receipt)


def build_durable_queue() -> DurableQueue:
    backend = settings.queue_backend.lower()
    if backend == "memory":
        return MemoryDurableQueue()
    if backend == "redis":
        return RedisDurableQueue(redis_url=settings.redis_url, queue_name=settings.redis_queue_name)
    if backend == "sqs":
        return SQSDurableQueue(queue_url=settings.sqs_queue_url, region=settings.aws_region)
    raise ValueError(f"unsupported queue backend: {settings.queue_backend}")


class ChunkResultStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[str, tuple[bool, dict]] = {}

    def set_success(self, task_id: str, key: str, etag: str | None) -> None:
        with self._lock:
            self._results[task_id] = (True, {"key": key, "etag": etag})

    def set_error(self, task_id: str, error: str) -> None:
        with self._lock:
            self._results[task_id] = (False, {"error": error})

    def wait(self, task_id: str, timeout_seconds: int) -> tuple[bool, dict] | None:
        deadline = time.time() + max(1, timeout_seconds)
        while time.time() < deadline:
            with self._lock:
                result = self._results.pop(task_id, None)
            if result is not None:
                return result
            time.sleep(0.02)
        return None
