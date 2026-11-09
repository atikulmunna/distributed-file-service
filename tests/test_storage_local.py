from pathlib import Path

from app.storage import LocalChunkStorage


def test_local_storage_write_and_read(tmp_path: Path) -> None:
    storage = LocalChunkStorage(str(tmp_path))

    result = storage.write_chunk("upload-123", 2, b"payload")
    assert result.key == "uploads/upload-123/chunk_2"
    assert result.etag is None
    assert storage.read_chunk(result.key) == b"payload"


def test_local_iter_object_streams_in_blocks(tmp_path) -> None:
    storage = LocalChunkStorage(str(tmp_path))
    key = storage.write_chunk("u1", 0, b"abcdefg").key
    blocks = list(storage.iter_object(key, block_size=3))
    assert blocks == [b"abc", b"def", b"g"]
