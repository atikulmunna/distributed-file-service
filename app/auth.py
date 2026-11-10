import hashlib
import hmac
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException
from jwt import InvalidTokenError

from app.config import AUTH_MODES, parse_api_key_mappings, settings
from app.metrics import count_throttled
from app.ratelimit import rate_limiter


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    api_key: str
    is_admin: bool = False


def secret_matches(presented: str, expected: str) -> bool:
    """Constant-time comparison of SHA-256 digests: equal lengths, so timing reveals neither how much
    of the secret matched nor its length."""
    return hmac.compare_digest(
        hashlib.sha256(presented.encode("utf-8")).digest(), hashlib.sha256(expected.encode("utf-8")).digest()
    )


def _user_for_api_key(presented: str) -> str | None:
    """Checks every configured key, with no early exit, so timing does not reveal which one matched."""
    found = None
    for api_key, user_id in parse_api_key_mappings(settings.api_key_mappings).items():
        if secret_matches(presented, api_key):
            found = user_id
    return found


def _parse_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="missing bearer token")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="invalid authorization header")
    token = authorization[len(prefix) :].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return token


def _resolve_from_jwt(authorization: str | None) -> str:
    if not settings.jwt_secret:
        raise HTTPException(status_code=500, detail="jwt auth is enabled but jwt_secret is not configured")
    token = _parse_bearer_token(authorization)
    try:
        payload = jwt.decode(
            token,
            key=settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience or None,  # None: the claim is not checked
            issuer=settings.jwt_issuer or None,
        )
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"invalid bearer token: {exc}") from exc
    user_id = str(payload.get("sub") or payload.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="jwt missing subject claim")
    return user_id


def _resolve_from_api_key(x_api_key: str | None) -> str:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing API key")
    user_id = _user_for_api_key(x_api_key)
    if not user_id:
        raise HTTPException(status_code=403, detail="invalid API key")
    return user_id


def enforce_rate_limit(user_id: str) -> None:
    """Keyed by user, not by credential: a new token, key or download link does not buy a new budget."""
    if not rate_limiter.allow(user_id, settings.api_rate_limit_per_minute, 60):
        count_throttled()
        raise HTTPException(
            status_code=429,
            detail="principal rate limit exceeded",
            headers={"Retry-After": "60", "X-RateLimit-Reason": "api_key_rate_limit"},
        )


def require_api_user(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> AuthUser:
    mode = settings.auth_mode.lower().strip()
    if mode not in AUTH_MODES:
        raise HTTPException(status_code=500, detail=f"unsupported auth_mode: {settings.auth_mode}")

    if mode == "jwt" or (mode == "hybrid" and authorization):
        user_id, source = _resolve_from_jwt(authorization), "jwt"
    else:
        user_id, source = _resolve_from_api_key(x_api_key), "api_key"

    enforce_rate_limit(user_id)
    admin_ids = {item.strip() for item in settings.admin_user_ids.split(",") if item.strip()}
    return AuthUser(user_id=user_id, api_key=source, is_admin=user_id in admin_ids)


def require_admin_user(user: AuthUser = Depends(require_api_user)) -> AuthUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin access required")
    return user
