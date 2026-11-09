from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException
from jwt import InvalidTokenError

from app.config import settings
from app.metrics import throttled_requests_total
from app.ratelimit import rate_limiter


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    api_key: str
    is_admin: bool = False


def _parse_api_key_mappings() -> dict[str, str]:
    mapping: dict[str, str] = {}
    raw = settings.api_key_mappings.strip()
    if not raw:
        return mapping

    for item in raw.split(","):
        pair = item.strip()
        if not pair:
            continue
        if ":" not in pair:
            continue
        api_key, user_id = pair.split(":", 1)
        api_key = api_key.strip()
        user_id = user_id.strip()
        if api_key and user_id:
            mapping[api_key] = user_id
    return mapping


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


def _resolve_from_jwt(authorization: str | None) -> tuple[str, str]:
    if not settings.jwt_secret:
        raise HTTPException(status_code=500, detail="jwt auth is enabled but jwt_secret is not configured")
    token = _parse_bearer_token(authorization)
    decode_kwargs = {
        "key": settings.jwt_secret,
        "algorithms": [settings.jwt_algorithm],
    }
    if settings.jwt_audience:
        decode_kwargs["audience"] = settings.jwt_audience
    if settings.jwt_issuer:
        decode_kwargs["issuer"] = settings.jwt_issuer
    try:
        payload = jwt.decode(token, **decode_kwargs)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"invalid bearer token: {exc}") from exc
    user_id = str(payload.get("sub") or payload.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="jwt missing subject claim")
    return user_id, token


def _resolve_from_api_key(x_api_key: str | None) -> tuple[str, str]:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing API key")
    mapping = _parse_api_key_mappings()
    user_id = mapping.get(x_api_key)
    if not user_id:
        raise HTTPException(status_code=403, detail="invalid API key")
    return user_id, x_api_key


def require_api_user(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> AuthUser:
    mode = settings.auth_mode.lower().strip()
    if mode not in {"api_key", "jwt", "hybrid"}:
        raise HTTPException(status_code=500, detail=f"unsupported auth_mode: {settings.auth_mode}")

    user_id: str
    rate_key: str
    source: str
    if mode == "jwt":
        user_id, rate_key = _resolve_from_jwt(authorization)
        source = "jwt"
    elif mode == "hybrid":
        if authorization:
            user_id, rate_key = _resolve_from_jwt(authorization)
            source = "jwt"
        else:
            user_id, rate_key = _resolve_from_api_key(x_api_key)
            source = "api_key"
    else:
        user_id, rate_key = _resolve_from_api_key(x_api_key)
        source = "api_key"

    # Keyed by user, not by credential: a new token or key does not buy a new budget.
    if not rate_limiter.allow(user_id, settings.api_rate_limit_per_minute, 60):
        throttled_requests_total.inc()
        raise HTTPException(
            status_code=429,
            detail="principal rate limit exceeded",
            headers={"Retry-After": "60", "X-RateLimit-Reason": "api_key_rate_limit"},
        )

    admin_ids = {item.strip() for item in settings.admin_user_ids.split(",") if item.strip()}
    return AuthUser(user_id=user_id, api_key=source, is_admin=user_id in admin_ids)


def require_admin_user(user: AuthUser = Depends(require_api_user)) -> AuthUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin access required")
    return user
