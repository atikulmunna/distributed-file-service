from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from app.config import settings


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


def require_api_user(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> AuthUser:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing API key")

    mapping = _parse_api_key_mappings()
    user_id = mapping.get(x_api_key)
    if not user_id:
        raise HTTPException(status_code=403, detail="invalid API key")
    admin_ids = {item.strip() for item in settings.admin_user_ids.split(",") if item.strip()}
    return AuthUser(user_id=user_id, api_key=x_api_key, is_admin=user_id in admin_ids)


def require_admin_user(user: AuthUser = Depends(require_api_user)) -> AuthUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin access required")
    return user
