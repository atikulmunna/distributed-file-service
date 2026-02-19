import time

from app.durable_queue import ChunkResultStore, ChunkWriteTask, MemoryDurableQueue


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
