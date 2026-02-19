from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import Base, SessionLocal, engine
from app.main import app
from app.maintenance import cleanup_once
from app.config import settings
from app.models import Chunk, ChunkRequestIdempotency, CompleteRequestIdempotency, InitRequestIdempotency, Upload


class _FakeStorage:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def delete_key(self, key: str) -> None:
        self.keys.discard(key)

    def list_keys(self, prefix: str = "") -> list[str]:
        return sorted([k for k in self.keys if k.startswith(prefix)])


def _reset_state() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.execute(delete(ChunkRequestIdempotency))
        db.execute(delete(CompleteRequestIdempotency))
        db.execute(delete(InitRequestIdempotency))
        db.execute(delete(Chunk))
        db.execute(delete(Upload))
        db.commit()


def test_cleanup_deletes_stale_uploads_and_orphans(monkeypatch) -> None:
    _reset_state()
    fake_storage = _FakeStorage()
    fake_storage.keys.update(
        {
            "uploads/stale-upload/chunk_0",
            "uploads/stale-upload/assembled",
            "uploads/orphan-upload/chunk_0",
        }
    )
    monkeypatch.setattr("app.maintenance.storage", fake_storage)

    old = datetime.now(timezone.utc) - timedelta(days=2)
    with SessionLocal() as db:
        stale = Upload(
            id="stale-upload",
            owner_id="dev-user",
            file_name="stale.bin",
            file_size=10,
            chunk_size=5,
            total_chunks=2,
            status="IN_PROGRESS",
            created_at=old,
            updated_at=old,
        )
        db.add(stale)
        db.add(
            Chunk(
                upload_id="stale-upload",
                chunk_index=0,
                size_bytes=5,
                s3_key="uploads/stale-upload/chunk_0",
                status="UPLOADED",
                retry_count=0,
                created_at=old,
                updated_at=old,
            )
        )
        db.add(
            InitRequestIdempotency(
                idempotency_key="old-key",
                upload_id="stale-upload",
                request_fingerprint="abc",
                created_at=old,
            )
        )
        db.commit()

    with SessionLocal() as db:
        stats = cleanup_once(db)
        assert stats["stale_uploads_deleted"] == 1
        assert stats["storage_keys_deleted"] >= 2

    assert "uploads/orphan-upload/chunk_0" not in fake_storage.keys


def test_admin_cleanup_endpoint_requires_api_key() -> None:
    with TestClient(app) as client:
        resp = client.post("/v1/admin/cleanup")
        assert resp.status_code == 401


def test_admin_cleanup_endpoint_requires_admin_role() -> None:
    old_mapping = settings.api_key_mappings
    old_admins = settings.admin_user_ids
    settings.api_key_mappings = "user-key:user-a,admin-key:user-admin"
    settings.admin_user_ids = "user-admin"
    try:
        with TestClient(app) as client:
            forbidden = client.post("/v1/admin/cleanup", headers={"X-API-Key": "user-key"})
            assert forbidden.status_code == 403

            allowed = client.post("/v1/admin/cleanup", headers={"X-API-Key": "admin-key"})
            assert allowed.status_code == 200
            assert allowed.json()["requested_by"] == "user-admin"
    finally:
        settings.api_key_mappings = old_mapping
        settings.admin_user_ids = old_admins
