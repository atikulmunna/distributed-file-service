from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Chunk,
    ChunkRequestIdempotency,
    CompleteRequestIdempotency,
    InitRequestIdempotency,
    Upload,
    UploadStatus,
)
from app.storage import storage


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _assembled_key(upload_id: str) -> str:
    return f"uploads/{upload_id}/assembled"


def cleanup_once(db: Session) -> dict[str, int]:
    now = _utc_now()
    stale_before = now - timedelta(seconds=settings.stale_upload_ttl_seconds)
    idempotency_before = now - timedelta(seconds=settings.idempotency_ttl_seconds)

    stale_uploads = list(
        db.scalars(
            select(Upload).where(
                Upload.status.in_([UploadStatus.initiated.value, UploadStatus.in_progress.value]),
                Upload.created_at < stale_before,
            )
        ).all()
    )

    deleted_storage_keys = 0
    stale_upload_ids: list[str] = []
    for upload in stale_uploads:
        stale_upload_ids.append(upload.id)
        chunk_keys = list(db.scalars(select(Chunk.s3_key).where(Chunk.upload_id == upload.id)).all())
        for key in chunk_keys:
            try:
                storage.delete_key(key)
                deleted_storage_keys += 1
            except Exception:
                # Best effort cleanup; keep DB cleanup moving.
                pass
        if settings.storage_backend.lower() in ("s3", "r2"):
            try:
                storage.delete_key(_assembled_key(upload.id))
                deleted_storage_keys += 1
            except Exception:
                pass

    if stale_upload_ids:
        db.execute(delete(Upload).where(Upload.id.in_(stale_upload_ids)))

    deleted_init = db.execute(
        delete(InitRequestIdempotency).where(InitRequestIdempotency.created_at < idempotency_before)
    ).rowcount or 0
    deleted_chunk = db.execute(
        delete(ChunkRequestIdempotency).where(ChunkRequestIdempotency.created_at < idempotency_before)
    ).rowcount or 0
    deleted_complete = db.execute(
        delete(CompleteRequestIdempotency).where(CompleteRequestIdempotency.created_at < idempotency_before)
    ).rowcount or 0

    referenced_keys = set(db.scalars(select(Chunk.s3_key)).all())
    if settings.storage_backend.lower() in ("s3", "r2"):
        for upload_id in db.scalars(select(Upload.id)).all():
            referenced_keys.add(_assembled_key(upload_id))

    orphan_deleted = 0
    try:
        for key in storage.list_keys("uploads/"):
            if key not in referenced_keys:
                try:
                    storage.delete_key(key)
                    orphan_deleted += 1
                except Exception:
                    pass
    except Exception:
        # Storage listing may be unavailable in some environments.
        pass

    db.commit()
    return {
        "stale_uploads_deleted": len(stale_upload_ids),
        "idempotency_rows_deleted": deleted_init + deleted_chunk + deleted_complete,
        "storage_keys_deleted": deleted_storage_keys + orphan_deleted,
    }
