"""Ownership checks shared by every upload endpoint."""

import hashlib

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.auth import AuthUser
from app.models import Upload


def get_owned_upload(db: Session, upload_id: str, user: AuthUser) -> Upload:
    upload = db.get(Upload, upload_id)
    # Another user's upload answers exactly like a missing one, so IDs cannot be probed for existence.
    if not upload or upload.owner_id != user.user_id:
        raise HTTPException(status_code=404, detail="upload not found")
    return upload


def scoped_idempotency_key(user: AuthUser, idempotency_key: str) -> str:
    """Idempotency keys belong to the user who sent them: two users choosing the same key never see
    each other's request. Stored as a fixed-length digest of (user, key)."""
    return hashlib.sha256(f"{user.user_id}\x00{idempotency_key}".encode()).hexdigest()
