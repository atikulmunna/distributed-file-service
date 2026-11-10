"""Secure defaults and error hygiene: production refuses development credentials, errors do not
reveal internals or which uploads exist, and operational endpoints can be closed."""

import hashlib
import hmac
import logging
import uuid

import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.auth as auth_module
from app.config import Settings, settings, validate_production_settings
from app.main import app
from app.storage import storage

OTHER_USER = {"X-API-Key": "other-key"}
SAFE_PRODUCTION = {"api_key_mappings": "prod-key:ops-user", "admin_user_ids": "ops-user", "auth_mode": "api_key"}


def _production(monkeypatch, **overrides) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    for name, value in {**SAFE_PRODUCTION, **overrides}.items():
        monkeypatch.setattr(settings, name, value)


def _init(client, headers, **extra_headers) -> str:
    response = client.post(
        "/v1/uploads/init",
        json={"file_name": "a.bin", "file_size": 4, "chunk_size": 4},
        headers={**headers, **extra_headers},
    )
    assert response.status_code == 201, response.text
    return response.json()["upload_id"]


# ---- production refuses development settings


def test_production_refuses_dev_key(monkeypatch) -> None:
    _production(monkeypatch, api_key_mappings="dev-key:dev-user")
    with pytest.raises(RuntimeError, match="dev-key"), TestClient(app):
        pass


def test_production_starts_with_safe_settings(monkeypatch) -> None:
    _production(monkeypatch)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_development_allows_dev_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "development")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"api_key_mappings": "prod-key:ops-user,dev-key:dev-user"}, "dev-key"),
        ({"admin_user_ids": "ops-user,dev-user"}, "dev-user"),
        ({"auth_mode": "jwt", "jwt_secret": "x" * 31}, "32 bytes"),
        ({"auth_mode": "hybrid", "jwt_secret": ""}, "32 bytes"),
        ({"auth_mode": "tokens"}, "AUTH_MODE"),
    ],
)
def test_validate_production_settings_reports_the_problem(overrides, expected) -> None:
    candidate = Settings(_env_file=None, **{**SAFE_PRODUCTION, **overrides})
    problems = validate_production_settings(candidate)
    assert len(problems) == 1, problems
    assert expected in problems[0]


def test_validate_production_settings_accepts_safe_settings() -> None:
    assert validate_production_settings(Settings(_env_file=None, **SAFE_PRODUCTION)) == []
    jwt = Settings(_env_file=None, **{**SAFE_PRODUCTION, "auth_mode": "jwt", "jwt_secret": "s" * 32})
    assert validate_production_settings(jwt) == []


def test_unknown_app_env_is_rejected() -> None:
    # A typo such as "prod" must not silently run with development defaults.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="prod")


# ---- uploads owned by someone else look exactly like missing ones


def test_not_owned_upload_is_404(client, auth_headers) -> None:
    upload_id = _init(client, auth_headers)
    missing_id = str(uuid.uuid4())
    calls = [
        ("GET", "/v1/uploads/{id}", {}),
        ("GET", "/v1/uploads/{id}/missing-chunks", {}),
        ("PUT", "/v1/uploads/{id}/chunks/0", {"content": b"abcd"}),
        ("POST", "/v1/uploads/{id}/complete", {}),
        ("GET", "/v1/uploads/{id}/download", {}),
        ("DELETE", "/v1/uploads/{id}", {}),
    ]
    for method, path, kwargs in calls:
        foreign = client.request(method, path.format(id=upload_id), headers=OTHER_USER, **kwargs)
        missing = client.request(method, path.format(id=missing_id), headers=OTHER_USER, **kwargs)
        assert foreign.status_code == 404, (method, path, foreign.text)
        assert foreign.json()["detail"] == missing.json()["detail"], (method, path)
    # and the owner's upload is untouched
    assert client.get(f"/v1/uploads/{upload_id}", headers=auth_headers).json()["status"] == "INITIATED"


def test_idempotency_key_is_scoped_to_its_owner(client, auth_headers) -> None:
    key = {"Idempotency-Key": "same-key"}
    mine = _init(client, auth_headers, **key)
    theirs = _init(client, OTHER_USER, **key)  # another user's key says nothing about mine
    assert theirs != mine
    assert _init(client, auth_headers, **key) == mine  # a replay still returns the original upload


def test_complete_idempotency_key_is_scoped_to_its_owner(client, auth_headers) -> None:
    key = {"Idempotency-Key": "complete-key"}
    mine = _init(client, auth_headers)
    theirs = _init(client, OTHER_USER)
    for upload_id, headers in ((mine, auth_headers), (theirs, OTHER_USER)):
        assert client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"abcd", headers=headers).status_code == 202
        response = client.post(f"/v1/uploads/{upload_id}/complete", headers={**headers, **key})
        assert response.status_code in (200, 202), response.text


def test_oversized_idempotency_key_is_rejected(client, auth_headers) -> None:
    response = client.post(
        "/v1/uploads/init",
        json={"file_name": "a.bin", "file_size": 4, "chunk_size": 4},
        headers={**auth_headers, "Idempotency-Key": "k" * 129},
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "validation_error"


# ---- error bodies


def test_500_body_has_no_exception_text(monkeypatch, auth_headers, caplog) -> None:
    def explode():
        raise RuntimeError("password=hunter2 host=db-internal:5432")

    monkeypatch.setattr("app.routes.ops.cleanup_once", explode)
    with caplog.at_level(logging.INFO, logger="dfs.request"), TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v1/admin/cleanup", headers=auth_headers)

    assert response.status_code == 500
    body = response.json()
    assert set(body) == {"detail", "error_code", "request_id"}
    assert body["detail"] == "internal error"
    assert body["error_code"] == "internal_error"
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    assert response.headers["X-Request-ID"] == body["request_id"]
    logged = [r.getMessage() for r in caplog.records if body["request_id"] in r.getMessage()]
    assert any("hunter2" in line and "RuntimeError" in line for line in logged), logged


def test_http_500_detail_is_not_exposed(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_mode", "jwt")
    monkeypatch.setattr(settings, "jwt_secret", "")
    response = client.get("/v1/uploads/anything", headers={"Authorization": "Bearer abc"})
    assert response.status_code == 500
    assert response.json() == {
        "detail": "internal error",
        "error_code": "internal_error",
        "request_id": response.headers["X-Request-ID"],
    }


def test_503_body_is_generic_and_keeps_retry_after(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(settings, "retry_base_delay_ms", 1)
    monkeypatch.setattr(settings, "retry_max_delay_ms", 2)
    upload_id = _init(client, auth_headers)

    def unreachable(*args, **kwargs):
        raise EndpointConnectionError(endpoint_url="http://internal-s3:9000")

    monkeypatch.setattr(storage, "write_chunk", unreachable)
    response = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"abcd", headers=auth_headers)
    assert response.status_code == 503
    assert response.headers["Retry-After"]
    body = response.json()
    assert set(body) == {"detail", "error_code", "request_id"}
    assert body["detail"] == "temporarily unavailable, retry later"
    assert body["error_code"] == "unavailable"


def test_validation_error_uses_the_error_envelope(client, auth_headers) -> None:
    response = client.post(
        "/v1/uploads/init", json={"file_name": "a.bin", "file_size": "not-a-number"}, headers=auth_headers
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "validation_error"
    assert body["detail"] == "request validation failed"
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["errors"]
    assert body["errors"][0]["loc"] == ["body", "file_size"]
    assert set(body["errors"][0]) == {"loc", "msg", "type"}  # the submitted value is not echoed back


# ---- authentication and operational endpoints


def test_api_key_lookup_compares_every_configured_key(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "api_key_mappings", "key-a:user-a,key-b:user-b,key-c:user-c")
    compared: list[tuple[bytes, bytes]] = []
    real_compare = hmac.compare_digest  # the patch below replaces it on the shared hmac module

    def spy(a, b):
        compared.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(auth_module.hmac, "compare_digest", spy)
    for presented, expected_status in (("key-a", 404), ("key-c", 404), ("nope", 403)):
        compared.clear()
        response = client.get("/v1/uploads/x", headers={"X-API-Key": presented})
        assert response.status_code == expected_status
        # no early exit: the match position does not change how many comparisons run
        assert len(compared) == 3
        # equal-length digests, so the comparison time does not depend on the key's length either
        assert all(len(a) == len(b) == hashlib.sha256().digest_size for a, b in compared)


def test_metrics_requires_token_when_configured(client, monkeypatch) -> None:
    assert client.get("/metrics").status_code == 200  # open when no token is set
    monkeypatch.setattr(settings, "metrics_token", "scrape-secret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    allowed = client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})
    assert allowed.status_code == 200
    assert "http_request_duration_seconds" in allowed.text


def test_version_minimal_in_production(monkeypatch) -> None:
    with TestClient(app) as client:
        assert "storage_backend" in client.get("/version").json()
    _production(monkeypatch)
    with TestClient(app) as client:
        assert set(client.get("/version").json()) == {"app_name", "app_version"}
