"""Signed download links: a short-lived token that grants a download of one upload, no API key needed.

Token: base64url(JSON {"u": upload_id, "o": owner_id, "exp": unix seconds}) + "." +
base64url(HMAC-SHA256(download_url_secret, first part)). Anyone holding the link can download until
it expires, so the TTL is short and links are kept out of the logs.
"""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import settings

# Development fallback when no secret is configured: links then work only within this process.
# Production refuses to start without DOWNLOAD_URL_SECRET (validate_production_settings).
_PROCESS_SECRET = secrets.token_bytes(32)


class InvalidDownloadLink(Exception):
    """Malformed, tampered with, signed with another secret, or expired."""


@dataclass(frozen=True)
class DownloadGrant:
    upload_id: str
    owner_id: str


def _secret() -> bytes:
    return settings.download_url_secret.encode("utf-8") or _PROCESS_SECRET


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(payload: str) -> str:
    return _b64(hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).digest())


def issue(upload_id: str, owner_id: str, now: float | None = None) -> tuple[str, int]:
    """A token for upload_id and the unix time it expires."""
    expires = int(now if now is not None else time.time()) + settings.download_url_ttl_seconds
    payload = _b64(json.dumps({"u": upload_id, "o": owner_id, "exp": expires}, separators=(",", ":")).encode())
    return f"{payload}.{_signature(payload)}", expires


def verify(token: str, now: float | None = None) -> DownloadGrant:
    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidDownloadLink("malformed")
    payload, signature = parts
    try:
        expected = _signature(payload)
    except UnicodeEncodeError as exc:
        raise InvalidDownloadLink("malformed") from exc
    if not hmac.compare_digest(signature.encode("ascii", "replace"), expected.encode("ascii")):
        raise InvalidDownloadLink("bad signature")
    try:
        claims = json.loads(_unb64(payload))
        grant = DownloadGrant(upload_id=str(claims["u"]), owner_id=str(claims["o"]))
        expires = int(claims["exp"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise InvalidDownloadLink("malformed") from exc
    if (now if now is not None else time.time()) >= expires:
        raise InvalidDownloadLink("expired")
    return grant


def expiry_datetime(expires: int) -> datetime:
    return datetime.fromtimestamp(expires, UTC)


def parse_expiry(value: str) -> float:
    """Unix time of an expires_at value as the API returns it (ISO 8601)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
