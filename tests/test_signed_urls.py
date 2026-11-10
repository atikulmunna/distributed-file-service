"""Signed download links: a short-lived URL that streams one completed upload without an API key."""

import logging
import time
from urllib.parse import urlparse

import pytest

from app import signed_urls
from app.config import Settings, settings, validate_production_settings
from app.ratelimit import rate_limiter

DATA = b"0123456789"
OTHER_USER = {"X-API-Key": "other-key"}


def _completed(client, headers, data: bytes = DATA) -> str:
    init = client.post(
        "/v1/uploads/init", json={"file_name": "report.bin", "file_size": len(data), "chunk_size": 4}, headers=headers
    )
    upload_id = init.json()["upload_id"]
    for index in range(0, len(data), 4):
        put = client.put(
            f"/v1/uploads/{upload_id}/chunks/{index // 4}", content=data[index : index + 4], headers=headers
        )
        assert put.status_code == 202, put.text
    assert client.post(f"/v1/uploads/{upload_id}/complete", headers=headers).json()["status"] == "COMPLETED"
    return upload_id


def _link(client, headers, upload_id: str) -> str:
    response = client.post(f"/v1/uploads/{upload_id}/download-url", headers=headers)
    assert response.status_code == 200, response.text
    return urlparse(response.json()["url"]).path


def test_download_url_round_trip(client, auth_headers) -> None:
    upload_id = _completed(client, auth_headers)
    response = client.post(f"/v1/uploads/{upload_id}/download-url", headers=auth_headers)
    body = response.json()
    assert body["url"].startswith("http")
    expires_in = signed_urls.parse_expiry(body["expires_at"]) - time.time()
    assert settings.download_url_ttl_seconds - 5 <= expires_in <= settings.download_url_ttl_seconds

    path = urlparse(body["url"]).path
    full = client.get(path)  # no API key: the link is the credential
    assert full.status_code == 200
    assert full.content == DATA
    assert 'filename="report.bin"' in full.headers["Content-Disposition"]
    assert full.headers["Cache-Control"] == "private, no-store"

    ranged = client.get(path, headers={"Range": "bytes=2-5"})
    assert ranged.status_code == 206
    assert ranged.content == DATA[2:6]
    assert ranged.headers["Content-Range"] == f"bytes 2-5/{len(DATA)}"


def test_download_url_expired_403(client, auth_headers) -> None:
    upload_id = _completed(client, auth_headers)
    token, _ = signed_urls.issue(upload_id, "dev-user", now=time.time() - settings.download_url_ttl_seconds - 1)
    response = client.get(f"/v1/downloads/{token}")
    assert response.status_code == 403
    assert response.json()["detail"] == "invalid or expired download link"


def test_download_url_tampered_403(client, auth_headers) -> None:
    upload_id = _completed(client, auth_headers)
    other_upload = _completed(client, auth_headers, data=b"secret")
    token = _link(client, auth_headers, upload_id).rsplit("/", 1)[1]
    payload, signature = token.split(".")

    swapped_payload, _ = signed_urls.issue(other_upload, "dev-user")
    forged = f"{swapped_payload.split('.')[0]}.{signature}"  # another upload's claims, this link's signature
    flipped = f"{payload}.{signature[:-1]}{'A' if signature[-1] != 'A' else 'B'}"
    for bad in (forged, flipped, payload, "not-a-token", f"{payload}.{signature}.extra"):
        response = client.get(f"/v1/downloads/{bad}")
        assert response.status_code == 403, bad


def test_download_url_signed_with_another_secret_403(client, auth_headers, monkeypatch) -> None:
    upload_id = _completed(client, auth_headers)
    path = _link(client, auth_headers, upload_id)
    monkeypatch.setattr(settings, "download_url_secret", "a-different-secret-of-at-least-32-bytes")
    assert client.get(path).status_code == 403


def test_download_url_requires_an_owned_completed_upload(client, auth_headers) -> None:
    init = client.post(
        "/v1/uploads/init", json={"file_name": "a", "file_size": 4, "chunk_size": 4}, headers=auth_headers
    )
    in_progress = init.json()["upload_id"]
    assert client.post(f"/v1/uploads/{in_progress}/download-url", headers=auth_headers).status_code == 409
    upload_id = _completed(client, auth_headers)
    assert client.post(f"/v1/uploads/{upload_id}/download-url", headers=OTHER_USER).status_code == 404
    assert client.post(f"/v1/uploads/{upload_id}/download-url").status_code == 401


def test_download_link_spends_the_owners_rate_limit(client, auth_headers, monkeypatch) -> None:
    upload_id = _completed(client, auth_headers)
    path = _link(client, auth_headers, upload_id)
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1)
    rate_limiter.reset()  # a fresh window: the owner has one request left
    assert client.get(path).status_code == 200
    blocked = client.get(path)  # no API key, but the link spends its owner's budget
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "60"


def test_signed_download_link_is_not_logged(client, auth_headers, caplog) -> None:
    upload_id = _completed(client, auth_headers)
    path = _link(client, auth_headers, upload_id)
    token = path.rsplit("/", 1)[1]
    with caplog.at_level(logging.INFO):
        assert client.get(path).status_code == 200
        assert client.get(path + "x").status_code == 403
    # The app's own logs (request and audit); the test client's httpx logger records its own requests.
    logged = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("dfs."))
    assert "/v1/downloads/{token}" in logged
    assert token not in logged


@pytest.mark.parametrize("secret", ["", "x" * 31])
def test_production_requires_a_download_url_secret(secret) -> None:
    candidate = Settings(
        _env_file=None,
        api_key_mappings="prod-key:ops",
        admin_user_ids="ops",
        download_url_secret=secret,
    )
    problems = validate_production_settings(candidate)
    assert problems == ["DOWNLOAD_URL_SECRET must be at least 32 bytes"]
