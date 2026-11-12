"""Two init requests with the same idempotency key at the same moment: both find no earlier request, and
both try to record the key. The second must answer as a replay would, not with a 500."""

from sqlalchemy import func, select

import app.routes.uploads as uploads_module
from app.config import settings
from app.db import SessionLocal
from app.models import Upload
from app.storage import storage

INIT = "/v1/uploads/init"
MIB = 1024 * 1024


def _lookup_misses_once(monkeypatch) -> None:
    """The next init looks up its key before the other request commits: the lookup finds nothing."""
    real = uploads_module._replay_init
    calls = {"n": 0}

    def lookup(*args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(*args, **kwargs)

    monkeypatch.setattr(uploads_module, "_replay_init", lookup)


def _upload_count() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Upload)) or 0


def test_racing_init_with_the_same_key_answers_with_the_first_upload(client, auth_headers, monkeypatch) -> None:
    body = {"file_name": "a.bin", "file_size": 10, "chunk_size": 5}
    headers = {**auth_headers, "Idempotency-Key": "race-key"}
    first = client.post(INIT, json=body, headers=headers)
    assert first.status_code == 201, first.text

    _lookup_misses_once(monkeypatch)
    second = client.post(INIT, json=body, headers=headers)
    assert second.status_code == 201, second.text
    assert second.json() == first.json()
    assert _upload_count() == 1  # the second request's own upload was rolled back


def test_racing_init_with_a_different_payload_is_a_conflict(client, auth_headers, monkeypatch) -> None:
    headers = {**auth_headers, "Idempotency-Key": "race-key"}
    assert (
        client.post(INIT, json={"file_name": "a.bin", "file_size": 10, "chunk_size": 5}, headers=headers).status_code
        == 201
    )

    _lookup_misses_once(monkeypatch)
    second = client.post(INIT, json={"file_name": "b.bin", "file_size": 10, "chunk_size": 5}, headers=headers)
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "idempotency key reused with different init payload"
    assert _upload_count() == 1


def test_racing_init_aborts_its_unused_multipart_upload(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(settings, "storage_backend", "s3")
    started: list[str] = []
    aborted: list[tuple[str, str]] = []

    def initialize_upload(upload_id: str) -> str:
        started.append(upload_id)
        return f"mp-{len(started)}"

    monkeypatch.setattr(storage, "initialize_upload", initialize_upload)
    monkeypatch.setattr(
        storage, "abort_upload", lambda upload_id, multipart_upload_id: aborted.append((upload_id, multipart_upload_id))
    )
    body = {"file_name": "big.bin", "file_size": 12 * MIB, "chunk_size": 5 * MIB}
    headers = {**auth_headers, "Idempotency-Key": "race-key"}
    first = client.post(INIT, json=body, headers=headers)
    assert first.status_code == 201, first.text

    _lookup_misses_once(monkeypatch)
    second = client.post(INIT, json=body, headers=headers)
    assert second.json()["upload_id"] == first.json()["upload_id"]
    assert len(started) == 2  # the losing request had already started its own multipart upload
    assert aborted == [(started[1], "mp-2")]
