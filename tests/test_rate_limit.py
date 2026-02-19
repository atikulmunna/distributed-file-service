from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.auth import api_key_rate_limiter
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
    api_key_rate_limiter.reset()


def test_api_key_rate_limit_enforced() -> None:
    _reset_state()
    old_limit = settings.api_rate_limit_per_minute
    settings.api_rate_limit_per_minute = 2
    try:
        with TestClient(app) as client:
            headers = {"X-API-Key": "dev-key"}
            body = {"file_name": "tiny.bin", "file_size": 1, "chunk_size": 10}
            first = client.post("/v1/uploads/init", json=body, headers=headers)
            second = client.post("/v1/uploads/init", json=body, headers=headers)
            third = client.post("/v1/uploads/init", json=body, headers=headers)

            assert first.status_code == 201
            assert second.status_code == 201
            assert third.status_code == 429
            assert third.headers.get("X-RateLimit-Reason") == "api_key_rate_limit"
            assert third.headers.get("Retry-After") == "60"
            payload = third.json()
            assert payload["error_code"] == "throttled"
    finally:
        settings.api_rate_limit_per_minute = old_limit
        api_key_rate_limiter.reset()
