from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Chunk, ChunkRequestIdempotency, CompleteRequestIdempotency, InitRequestIdempotency, Upload


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


def test_missing_api_key_rejected() -> None:
    _reset_state()
    with TestClient(app) as client:
        response = client.post("/v1/uploads/init", json={"file_name": "a.bin", "file_size": 4, "chunk_size": 4})
        assert response.status_code == 401


def test_upload_owner_enforced_between_users() -> None:
    _reset_state()
    old_mapping = settings.api_key_mappings
    settings.api_key_mappings = "key-a:user-a,key-b:user-b"
    try:
        with TestClient(app) as client:
            init = client.post(
                "/v1/uploads/init",
                json={"file_name": "a.bin", "file_size": 4, "chunk_size": 4},
                headers={"X-API-Key": "key-a"},
            )
            assert init.status_code == 201
            upload_id = init.json()["upload_id"]

            forbidden = client.get(
                f"/v1/uploads/{upload_id}/missing-chunks",
                headers={"X-API-Key": "key-b"},
            )
            assert forbidden.status_code == 403
    finally:
        settings.api_key_mappings = old_mapping
