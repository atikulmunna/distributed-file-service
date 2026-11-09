import time

from fastapi.testclient import TestClient

from app.config import settings
from app.durable_queue import ChunkResultStore, ChunkWriteTask, MemoryDurableQueue
from app.main import app


def test_chunk_task_roundtrip_serialization() -> None:
    task = ChunkWriteTask.from_bytes(upload_id="u1", chunk_index=3, data=b"hello", multipart_upload_id="m1")
    payload = task.to_json()
    restored = ChunkWriteTask.from_json(payload)
    assert restored.task_id == task.task_id
    assert restored.upload_id == "u1"
    assert restored.chunk_index == 3
    assert restored.multipart_upload_id == "m1"
    assert restored.data() == b"hello"


def test_memory_queue_enqueue_dequeue() -> None:
    queue = MemoryDurableQueue()
    task = ChunkWriteTask.from_bytes(upload_id="u2", chunk_index=0, data=b"x")
    queue.enqueue(task)
    message = queue.dequeue(timeout_seconds=1)
    assert message is not None
    assert message.task.task_id == task.task_id
    queue.ack(message.receipt)


def test_result_store_success_then_wait() -> None:
    store = ChunkResultStore()
    task_id = "t1"
    store.set_success(task_id, key="uploads/u1/chunk_0", etag="etag-1")
    result = store.wait(task_id, timeout_seconds=1)
    assert result is not None
    success, payload = result
    assert success is True
    assert payload["key"].endswith("chunk_0")


def test_result_store_timeout() -> None:
    store = ChunkResultStore()
    start = time.time()
    result = store.wait("missing", timeout_seconds=1)
    elapsed = time.time() - start
    assert result is None
    assert elapsed >= 1


def test_durable_queue_mode_chunk_put_succeeds_in_process(auth_headers, monkeypatch) -> None:
    # Regression: the request used to wait for the consumer on the event loop, so the consumer
    # (same loop) never ran and every chunk timed out with 504.
    monkeypatch.setattr(settings, "queue_backend", "redis")
    monkeypatch.setattr(settings, "queue_task_timeout_seconds", 2)
    monkeypatch.setattr(settings, "queue_poll_timeout_seconds", 1)
    with TestClient(app) as client:
        init = client.post(
            "/v1/uploads/init", json={"file_name": "q.bin", "file_size": 10, "chunk_size": 10}, headers=auth_headers
        )
        assert init.status_code == 201, init.text
        upload_id = init.json()["upload_id"]
        assert client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"y" * 10, headers=auth_headers).status_code == 202
