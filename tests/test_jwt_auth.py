import jwt
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


def test_jwt_mode_accepts_valid_bearer_token() -> None:
    _reset_state()
    old_mode = settings.auth_mode
    old_secret = settings.jwt_secret
    old_algo = settings.jwt_algorithm
    old_aud = settings.jwt_audience
    old_iss = settings.jwt_issuer
    settings.auth_mode = "jwt"
    settings.jwt_secret = "test-secret"
    settings.jwt_algorithm = "HS256"
    settings.jwt_audience = ""
    settings.jwt_issuer = ""
    try:
        token = jwt.encode({"sub": "jwt-user"}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
        with TestClient(app) as client:
            response = client.post(
                "/v1/uploads/init",
                json={"file_name": "jwt.bin", "file_size": 10, "chunk_size": 5},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 201, response.text
    finally:
        settings.auth_mode = old_mode
        settings.jwt_secret = old_secret
        settings.jwt_algorithm = old_algo
        settings.jwt_audience = old_aud
        settings.jwt_issuer = old_iss


def test_jwt_mode_rejects_invalid_token() -> None:
    _reset_state()
    old_mode = settings.auth_mode
    old_secret = settings.jwt_secret
    settings.auth_mode = "jwt"
    settings.jwt_secret = "test-secret"
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/uploads/init",
                json={"file_name": "jwt.bin", "file_size": 10, "chunk_size": 5},
                headers={"Authorization": "Bearer bad.token.value"},
            )
            assert response.status_code == 401
            assert response.json()["error_code"] == "missing_api_key"
    finally:
        settings.auth_mode = old_mode
        settings.jwt_secret = old_secret


def test_hybrid_mode_keeps_api_key_fallback() -> None:
    _reset_state()
    old_mode = settings.auth_mode
    settings.auth_mode = "hybrid"
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/uploads/init",
                json={"file_name": "hybrid.bin", "file_size": 10, "chunk_size": 5},
                headers={"X-API-Key": "dev-key"},
            )
            assert response.status_code == 201
    finally:
        settings.auth_mode = old_mode
