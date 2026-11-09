"""Input bounds on init, and chunk bodies checked against the upload's declared layout."""

import asyncio

from sqlalchemy import select, update

from app.config import settings
from app.db import SessionLocal
from app.main import app, upload_limiter
from app.models import Chunk
from app.storage import storage

MIB = 1024 * 1024
S3_SMALL_PART_REASON = "chunk_size must be at least 5 MiB (5242880 bytes) when an upload has more than one chunk on the s3 backend"


def _init(client, headers, file_size: int, chunk_size: int | None, file_name: str = "f.bin"):
    payload = {"file_name": file_name, "file_size": file_size}
    if chunk_size is not None:
        payload["chunk_size"] = chunk_size
    return client.post("/v1/uploads/init", json=payload, headers=headers)


def _new_upload(client, headers, file_size: int, chunk_size: int) -> str:
    response = _init(client, headers, file_size, chunk_size)
    assert response.status_code == 201, response.text
    return response.json()["upload_id"]


# ---------------------------------------------------------------- init bounds


def test_init_rejects_too_many_chunks(client, auth_headers) -> None:
    response = _init(client, auth_headers, file_size=10**9, chunk_size=1)
    assert response.status_code == 400
    assert "chunks" in response.json()["detail"]


def test_init_rejects_oversized_file(client, auth_headers) -> None:
    response = _init(client, auth_headers, file_size=settings.max_file_size_bytes + 1, chunk_size=64 * MIB)
    assert response.status_code == 400
    assert "file_size" in response.json()["detail"]


def test_init_rejects_chunk_size_out_of_bounds(client, auth_headers) -> None:
    response = _init(client, auth_headers, file_size=10, chunk_size=settings.max_chunk_size_bytes + 1)
    assert response.status_code == 400
    assert "chunk_size" in response.json()["detail"]


def test_s3_small_multi_chunk_rejected(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(settings, "storage_backend", "s3")
    response = _init(client, auth_headers, file_size=10, chunk_size=5)
    assert response.status_code == 400
    assert response.json()["detail"] == S3_SMALL_PART_REASON


def test_s3_single_small_chunk_allowed(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(settings, "storage_backend", "s3")
    assert _init(client, auth_headers, file_size=5, chunk_size=5).status_code == 201


def test_empty_file_init_allowed(client, auth_headers) -> None:
    response = _init(client, auth_headers, file_size=0, chunk_size=5)
    assert response.status_code == 201
    assert response.json()["total_chunks"] == 0


def test_control_chars_in_file_name_rejected(client, auth_headers) -> None:
    for bad_name in ("bad\nname.txt", "tab\there", "nul\x00", "c1\x85"):
        assert _init(client, auth_headers, 10, 5, file_name=bad_name).status_code == 422, repr(bad_name)


def test_file_name_length_limit(client, auth_headers) -> None:
    assert _init(client, auth_headers, 10, 5, file_name="a" * 255).status_code == 201
    assert _init(client, auth_headers, 10, 5, file_name="a" * 256).status_code == 422


# ---------------------------------------------------------------- chunk body vs layout


def test_chunk_size_must_match_layout(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 10, 5)
    response = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"A" * 100, headers=auth_headers)
    assert response.status_code == 400
    assert "expected 5 bytes" in response.json()["detail"]


def test_last_chunk_must_have_its_exact_size(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 11, 5)
    assert client.put(f"/v1/uploads/{upload_id}/chunks/2", content=b"zz", headers=auth_headers).status_code == 400
    assert client.put(f"/v1/uploads/{upload_id}/chunks/2", content=b"z", headers=auth_headers).status_code == 202


def test_missing_content_length_411(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 10, 5)

    def chunked_body():  # an iterator body is sent with Transfer-Encoding: chunked, no Content-Length
        yield b"abcde"

    response = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=chunked_body(), headers=auth_headers)
    assert response.status_code == 411


def test_complete_rejects_size_sum_mismatch(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 10, 5)
    for index, body in enumerate((b"aaaaa", b"bbbbb")):
        assert client.put(f"/v1/uploads/{upload_id}/chunks/{index}", content=body, headers=auth_headers).status_code == 202
    with SessionLocal() as db:  # a row written before layout checks existed
        db.execute(update(Chunk).where(Chunk.upload_id == upload_id, Chunk.chunk_index == 1).values(size_bytes=3))
        db.commit()
    response = client.post(f"/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert response.status_code == 409
    assert "add up to 8 bytes" in response.json()["detail"]


# ---------------------------------------------------------------- raw ASGI: body timing


def _chunk_put_scope(upload_id: str, content_length: int) -> dict:
    path = f"/v1/uploads/{upload_id}/chunks/0"
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"x-api-key", b"dev-key"), (b"content-length", str(content_length).encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


def _run_asgi(scope: dict, receive, timeout_s: float = 10.0) -> int:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def call() -> None:
        await asyncio.wait_for(app(scope, receive, send), timeout=timeout_s)

    asyncio.run(call())
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _chunk_rows(upload_id: str) -> int:
    with SessionLocal() as db:
        return len(list(db.scalars(select(Chunk).where(Chunk.upload_id == upload_id))))


def test_client_disconnect_mid_body_records_nothing(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 10, 5)
    messages = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive() -> dict:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    status = _run_asgi(_chunk_put_scope(upload_id, 5), receive)
    assert status == 499  # client closed the request; logged as a client error, not a 500
    assert _chunk_rows(upload_id) == 0
    assert upload_limiter._counts.get(upload_id, 0) == 0
    assert storage.list_keys(f"uploads/{upload_id}/") == []


def test_slow_body_times_out_without_holding_slot(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(settings, "chunk_body_timeout_seconds", 1, raising=False)
    upload_id = _new_upload(client, auth_headers, 10, 5)
    limiter_counts_while_stalled: list[int] = []
    calls = 0

    async def receive() -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b"ab", "more_body": True}
        limiter_counts_while_stalled.append(upload_limiter._counts.get(upload_id, 0))
        await asyncio.Event().wait()  # the client never sends the rest

    status = _run_asgi(_chunk_put_scope(upload_id, 5), receive, timeout_s=5.0)
    assert status == 408
    assert limiter_counts_while_stalled and set(limiter_counts_while_stalled) == {0}
    assert upload_limiter._counts.get(upload_id, 0) == 0
    assert _chunk_rows(upload_id) == 0


def test_body_longer_than_content_length_rejected(client, auth_headers) -> None:
    upload_id = _new_upload(client, auth_headers, 10, 5)
    messages = [{"type": "http.request", "body": b"abcdefghij", "more_body": False}]

    async def receive() -> dict:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    assert _run_asgi(_chunk_put_scope(upload_id, 5), receive) == 400
    assert _chunk_rows(upload_id) == 0
