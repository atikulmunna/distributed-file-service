"""Downloads: correct ranges and headers, reading only the needed bytes, ordered parallel prefetch,
and no DB connection held while streaming."""

import random
import threading
import time
import types

from app.config import settings
from app.db import engine
from app.download import Piece, iter_pieces, object_pieces
from app.layout import ChunkLayout
from app.storage import storage

NAME = "রিপোর্ট.txt"  # Bengali "report"


def _completed(client, headers, data: bytes, chunk_size: int, file_name: str = "f.bin") -> str:
    init = client.post(
        "/v1/uploads/init", json={"file_name": file_name, "file_size": len(data), "chunk_size": chunk_size}, headers=headers
    )
    assert init.status_code == 201, init.text
    upload_id = init.json()["upload_id"]
    for index in range(0, len(data), chunk_size):
        response = client.put(
            f"/v1/uploads/{upload_id}/chunks/{index // chunk_size}", content=data[index : index + chunk_size], headers=headers
        )
        assert response.status_code == 202, response.text
    assert client.post(f"/v1/uploads/{upload_id}/complete", headers=headers).status_code == 200
    return upload_id


def _get(client, headers, upload_id: str, range_header: str | None = None):
    extra = {"Range": range_header} if range_header else {}
    return client.get(f"/v1/uploads/{upload_id}/download", headers={**headers, **extra})


# ---------------------------------------------------------------- ranges and headers


def test_range_suffix_returns_tail(client, auth_headers) -> None:
    upload_id = _completed(client, auth_headers, b"0123456789", 10)
    response = _get(client, auth_headers, upload_id, "bytes=-3")
    assert response.status_code == 206
    assert response.content == b"789"
    assert response.headers["Content-Range"] == "bytes 7-9/10"


def test_malformed_range_does_not_500(client, auth_headers) -> None:
    upload_id = _completed(client, auth_headers, b"0123456789", 10)
    for bad in ("bytes=abc-", "bytes=0-1,4-5", "items=0-1"):
        response = _get(client, auth_headers, upload_id, bad)
        assert response.status_code == 200, bad
        assert response.content == b"0123456789"


def test_range_end_past_the_file_is_clamped(client, auth_headers) -> None:
    upload_id = _completed(client, auth_headers, b"0123456789", 10)
    response = _get(client, auth_headers, upload_id, "bytes=5-999")
    assert response.status_code == 206
    assert response.content == b"56789"
    assert response.headers["Content-Range"] == "bytes 5-9/10"


def test_unsatisfiable_range_416_with_content_range(client, auth_headers) -> None:
    upload_id = _completed(client, auth_headers, b"0123456789", 10)
    response = _get(client, auth_headers, upload_id, "bytes=10-")
    assert response.status_code == 416
    assert response.headers["Content-Range"] == "bytes */10"


def test_non_ascii_filename_download(client, auth_headers) -> None:
    upload_id = _completed(client, auth_headers, b"data", 4, file_name=NAME)
    response = _get(client, auth_headers, upload_id)
    assert response.status_code == 200
    assert response.content == b"data"
    assert "filename*=UTF-8''%E0%A6%B0" in response.headers["Content-Disposition"]


def test_download_sets_content_length(client, auth_headers) -> None:
    upload_id = _completed(client, auth_headers, b"0123456789", 4)
    full = _get(client, auth_headers, upload_id)
    assert full.headers["Content-Length"] == "10"
    assert full.headers["Accept-Ranges"] == "bytes"
    partial = _get(client, auth_headers, upload_id, "bytes=3-8")
    assert partial.headers["Content-Length"] == "6"
    assert partial.content == b"345678"


def test_empty_file_download_has_zero_length(client, auth_headers) -> None:
    upload_id = _completed(client, auth_headers, b"", 5)
    response = _get(client, auth_headers, upload_id)
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["Content-Length"] == "0"


# ---------------------------------------------------------------- reading only what is needed


def test_range_near_end_reads_only_needed_bytes(client, auth_headers, monkeypatch) -> None:
    data = bytes(range(20))
    upload_id = _completed(client, auth_headers, data, 5)  # chunks of 5: offsets 0, 5, 10, 15
    reads: list[tuple[str, int, int]] = []
    real_read = storage.read_range

    def spy(key: str, start: int, end: int) -> bytes:
        reads.append((key, start, end))
        return real_read(key, start, end)

    monkeypatch.setattr(storage, "read_range", spy)
    response = _get(client, auth_headers, upload_id, "bytes=17-19")
    assert response.status_code == 206 and response.content == data[17:20]
    assert reads == [(f"uploads/{upload_id}/chunk_3", 2, 4)]


def test_range_across_chunk_boundaries(client, auth_headers) -> None:
    data = bytes(range(23))
    upload_id = _completed(client, auth_headers, data, 5)
    response = _get(client, auth_headers, upload_id, "bytes=3-21")
    assert response.status_code == 206 and response.content == data[3:22]


def test_no_db_connection_held_while_streaming(client, auth_headers, monkeypatch) -> None:
    upload_id = _completed(client, auth_headers, b"0123456789", 5)
    held: list[int] = []
    real_read = storage.read_range

    def observed(key: str, start: int, end: int) -> bytes:
        held.append(engine.pool.checkedout())
        return real_read(key, start, end)

    monkeypatch.setattr(storage, "read_range", observed)
    assert _get(client, auth_headers, upload_id).status_code == 200
    assert held and set(held) == {0}


def test_object_pieces_for_chunk_files_and_assembled_objects(monkeypatch) -> None:
    layout = ChunkLayout(11, 5)
    chunks = [types.SimpleNamespace(chunk_index=i, s3_key=f"k{i}") for i in range(3)]
    local = types.SimpleNamespace(id="u1", multipart_upload_id=None)
    assert object_pieces(local, chunks, layout, 3, 9) == [Piece("k0", 3, 4), Piece("k1", 0, 4)]
    assert object_pieces(local, chunks, layout, 4, 10) == [Piece("k0", 4, 4), Piece("k1", 0, 4), Piece("k2", 0, 0)]

    monkeypatch.setattr(settings, "storage_backend", "s3")
    multipart = types.SimpleNamespace(id="u1", multipart_upload_id="mp")
    assert object_pieces(multipart, chunks, layout, 3, 9) == [Piece("uploads/u1/assembled", 3, 9)]


# ---------------------------------------------------------------- parallel prefetch


class _SlowObjects:
    """Fake storage: each object is a run of one repeated byte; reads sleep a random time."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0

    def read_range(self, key: str, start: int, end: int) -> bytes:
        with self.lock:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        time.sleep(random.uniform(0.001, 0.02))
        with self.lock:
            self.in_flight -= 1
        return key.encode() * (end - start + 1)


def test_prefetch_preserves_order(monkeypatch) -> None:
    fake = _SlowObjects()
    monkeypatch.setattr(storage, "read_range", fake.read_range)
    pieces = [Piece("a", 0, 9), Piece("b", 0, 4), Piece("c", 3, 12)]
    output = b"".join(iter_pieces(pieces, segment_bytes=3, prefetch=4))
    assert output == b"a" * 10 + b"b" * 5 + b"c" * 10
    assert fake.max_in_flight <= 4  # memory bound: at most prefetch segments in flight
    assert fake.max_in_flight > 1  # and they really run in parallel


def test_prefetch_stops_when_the_client_goes_away(monkeypatch) -> None:
    fake = _SlowObjects()
    monkeypatch.setattr(storage, "read_range", fake.read_range)
    stream = iter_pieces([Piece("a", 0, 999)], segment_bytes=10, prefetch=4)  # 100 segments
    next(stream)
    stream.close()  # the response is abandoned after the first segment
    time.sleep(0.1)
    assert fake.calls <= 4 + 1
