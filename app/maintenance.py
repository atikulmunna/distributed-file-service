"""Cleanup of abandoned uploads, expired idempotency keys, and orphaned objects. Built not to lose data:

- Staleness is judged by last_activity_at (last chunk or status change), never by created_at, so a long
  upload that keeps sending chunks is never touched.
- An abandoned upload is first claimed with a conditional UPDATE to ABORTED that re-checks it is still
  inactive; from then on no chunk can be recorded and no complete can claim it. Only then is storage
  cleaned (multipart upload aborted, objects deleted), with no DB connection held, and the row deleted.
  If the multipart abort fails, the row is kept: it is the only reference to the parts. The claim does
  not touch last_activity_at, so the next run retries it.
- VERIFYING and COMPLETED uploads are never deleted (the server already accepted them); VERIFYING
  uploads inactive past the TTL are reported for alerting instead.
- Unreferenced objects are deleted only when older than orphan_grace_seconds, so a chunk whose DB row
  is not committed yet survives.
- One instance at a time: a Postgres session-level advisory lock on a dedicated connection (no
  transaction stays open during storage calls). Other instances skip the run.
- Every error is logged (cleanup_error) and counted (cleanup_errors_total{step}); the run continues.
Multipart uploads with no DB row at all (a crash between CreateMultipartUpload and the insert) are
invisible to listings; the bucket lifecycle rule AbortIncompleteMultipartUpload covers those.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, text, update

from app.config import settings
from app.db import SessionLocal, engine
from app.logging_utils import log_event
from app.metrics import cleanup_errors_total, uploads_stuck_verifying
from app.models import (
    Chunk,
    ChunkRequestIdempotency,
    CompleteRequestIdempotency,
    InitRequestIdempotency,
    Upload,
    UploadStatus,
    utc_now,
)
from app.storage import storage

CLEANUP_LOCK_KEY = 0x64667363  # app-wide advisory lock id ("dfsc")
STALE_STATUSES = [
    UploadStatus.initiated.value,
    UploadStatus.in_progress.value,
    UploadStatus.completing.value,
    UploadStatus.failed.value,
    UploadStatus.aborted.value,
]


@contextmanager
def cleanup_lock() -> Iterator[bool]:
    """True when this process may run cleanup. Postgres: session-level advisory lock held on a
    dedicated connection, released explicitly or when the connection drops. Elsewhere: always True."""
    if engine.dialect.name != "postgresql":
        yield True
        return
    with engine.connect() as conn:
        acquired = bool(conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": CLEANUP_LOCK_KEY}))
        conn.commit()  # hold the lock without keeping a transaction open
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": CLEANUP_LOCK_KEY})
                conn.commit()


def _error(stats: dict, step: str, exc: Exception, **context) -> None:
    stats["errors"] += 1
    cleanup_errors_total.labels(step=step).inc()
    log_event({"event": "cleanup_error", "step": step, "error_class": type(exc).__name__, "detail": str(exc), **context})


def _uses_assembled_objects() -> bool:
    return settings.storage_backend.lower() in ("s3", "r2")


def _claim_abandoned(upload_id: str, stale_before: datetime) -> tuple[bool, str | None, list[str]]:
    """Move one stale upload to ABORTED if it is still inactive. Returns (claimed, multipart id, keys)."""
    reason = f"abandoned: no activity for {settings.stale_upload_ttl_seconds} s"
    with SessionLocal() as db:
        claimed = (
            db.execute(
                update(Upload)
                .where(
                    Upload.id == upload_id,
                    Upload.status.in_(STALE_STATUSES),
                    Upload.last_activity_at < stale_before,
                )
                .values(
                    status=UploadStatus.aborted.value,
                    updated_at=utc_now(),
                    failure_reason=func.coalesce(Upload.failure_reason, reason),
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            == 1
        )
        if not claimed:
            db.rollback()
            return False, None, []
        multipart_upload_id = db.scalar(select(Upload.multipart_upload_id).where(Upload.id == upload_id))
        keys = list(db.scalars(select(Chunk.s3_key).where(Chunk.upload_id == upload_id)))
        db.commit()
    return True, multipart_upload_id, keys


def _clean_stale_uploads(now: datetime, stats: dict) -> None:
    stale_before = now - timedelta(seconds=settings.stale_upload_ttl_seconds)
    with SessionLocal() as db:
        candidates = list(
            db.scalars(
                select(Upload.id).where(Upload.status.in_(STALE_STATUSES), Upload.last_activity_at < stale_before)
            )
        )
    for upload_id in candidates:
        claimed, multipart_upload_id, keys = _claim_abandoned(upload_id, stale_before)
        if not claimed:
            continue  # it became active again since it was listed
        if multipart_upload_id:
            try:
                storage.abort_upload(upload_id, multipart_upload_id)
                stats["multipart_uploads_aborted"] += 1
            except Exception as exc:
                _error(stats, "abort_multipart", exc, upload_id=upload_id)
                continue  # keep the row: it is the only reference to the parts; the next run retries
        if _uses_assembled_objects():
            keys.append(storage.assembled_key(upload_id))
        for key in sorted(set(keys)):
            try:
                storage.delete_key(key)
                stats["storage_keys_deleted"] += 1
            except Exception as exc:
                _error(stats, "delete_object", exc, upload_id=upload_id, key=key)  # an orphan now; a later run removes it
        with SessionLocal() as db:
            stats["stale_uploads_deleted"] += db.execute(delete(Upload).where(Upload.id == upload_id)).rowcount or 0
            db.commit()


def _clean_idempotency(now: datetime, stats: dict) -> None:
    before = now - timedelta(seconds=settings.idempotency_ttl_seconds)
    with SessionLocal() as db:
        for model in (InitRequestIdempotency, ChunkRequestIdempotency, CompleteRequestIdempotency):
            stats["idempotency_rows_deleted"] += db.execute(delete(model).where(model.created_at < before)).rowcount or 0
        db.commit()


def _clean_orphans(now: datetime, stats: dict) -> None:
    grace_before = now - timedelta(seconds=settings.orphan_grace_seconds)
    with SessionLocal() as db:
        referenced = set(db.scalars(select(Chunk.s3_key)))
        if _uses_assembled_objects():
            referenced.update(storage.assembled_key(upload_id) for upload_id in db.scalars(select(Upload.id)))
    try:
        objects = storage.list_objects("uploads/")
    except Exception as exc:
        _error(stats, "list_objects", exc)
        return
    for stored in objects:
        if stored.key in referenced or stored.last_modified >= grace_before:
            continue
        try:
            storage.delete_key(stored.key)
            stats["orphans_deleted"] += 1
        except Exception as exc:
            _error(stats, "delete_object", exc, key=stored.key)


def _report_stuck_verifications(now: datetime, stats: dict) -> None:
    stale_before = now - timedelta(seconds=settings.stale_upload_ttl_seconds)
    with SessionLocal() as db:
        stuck = db.scalar(
            select(func.count())
            .select_from(Upload)
            .where(Upload.status == UploadStatus.verifying.value, Upload.last_activity_at < stale_before)
        )
    stats["uploads_stuck_verifying"] = stuck or 0
    uploads_stuck_verifying.set(stats["uploads_stuck_verifying"])
    if stuck:
        log_event({"event": "verifications_stuck", "count": stuck})


def cleanup_once() -> dict:
    stats = {
        "skipped": False,
        "stale_uploads_deleted": 0,
        "multipart_uploads_aborted": 0,
        "storage_keys_deleted": 0,
        "orphans_deleted": 0,
        "idempotency_rows_deleted": 0,
        "uploads_stuck_verifying": 0,
        "errors": 0,
    }
    with cleanup_lock() as acquired:
        if not acquired:
            stats["skipped"] = True
            log_event({"event": "cleanup_skipped", "reason": "another instance holds the cleanup lock"})
            return stats
        now = utc_now()
        for step in (_clean_stale_uploads, _clean_idempotency, _clean_orphans, _report_stuck_verifications):
            try:
                step(now, stats)
            except Exception as exc:  # one failing step must not stop the others
                _error(stats, step.__name__.lstrip("_"), exc)
    log_event({"event": "cleanup_finished", **stats})
    return stats
