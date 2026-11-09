"""Ownership checks shared by every upload endpoint."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.auth import AuthUser
from app.models import Upload


def get_owned_upload(db: Session, upload_id: str, user: AuthUser) -> Upload:
    upload = db.get(Upload, upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="upload not found")
    if upload.owner_id != user.user_id:
        raise HTTPException(status_code=403, detail="forbidden for this upload owner")
    return upload
