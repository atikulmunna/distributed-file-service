from dataclasses import dataclass

from fastapi import Header, HTTPException

from app.config import settings


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    api_key: str


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


def require_api_user(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> AuthUser:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing API key")

    mapping = _parse_api_key_mappings()
    user_id = mapping.get(x_api_key)
    if not user_id:
        raise HTTPException(status_code=403, detail="invalid API key")
    return AuthUser(user_id=user_id, api_key=x_api_key)
