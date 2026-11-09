"""Completing and aborting uploads.

Completion runs in three phases so that no DB connection is held during storage work:
  1. claim (short transaction): move IN_PROGRESS to COMPLETING with a conditional UPDATE (only one
     concurrent request can win), then read and validate the chunk list under that claim;
  2. storage work (no DB connection): complete the multipart upload;
  3. finish (short transaction): COMPLETING to COMPLETED, or to VERIFYING when the client declared a
     whole-file checksum; a verification job then checks it asynchronously (app/finalizer.py).
If storage work fails, the claim is released back to IN_PROGRESS so the client can retry. If the
process dies after storage work but before finishing, the upload stays COMPLETING until its lease
(completion_lease_seconds) expires; another complete request may then take it over, and treats an
already assembled object as success.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.auth import AuthUser
from app.config import settings
from app.db import SessionLocal, dialect_insert
from app.jobs import FinalizeJob, job_queue
from app.logging_utils import log_event
from app.models import (
    Chunk,
    ChunkStatus,
    CompleteRequestIdempotency,
    Upload,
    UploadStatus,
    utc_now,
)
from app.schemas import CompleteUploadResponse
from app.storage import storage
from app.storage_policy import with_storage_retries
from app.upload_access import get_owned_upload
from app.upload_state import transition

ABORT_REASON = "aborted by client"


@dataclass(frozen=True)
class CompletionWork:
    upload_id: str
    file_checksum_sha256: str | None
    multipart_upload_id: str | None
    chunk_keys: list[str] = field(default_factory=list)
    parts: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class CompletionReplay:
    response: CompleteUploadResponse
    already_completed: bool  # True when the upload was already COMPLETED or VERIFYING (audited as a replay)


def _record_complete_key(db, idempotency_key: str, upload_id: str, fingerprint: str) -> None:
    statement = dialect_insert(db)(CompleteRequestIdempotency).values(
        idempotency_key=idempotency_key, upload_id=upload_id, request_fingerprint=fingerprint
    )
    db.execute(statement.on_conflict_do_nothing(index_elements=[CompleteRequestIdempotency.idempotency_key]))


def claim_completion(
    upload_id: str, user: AuthUser, idempotency_key: str | None, fingerprint: str
) -> CompletionWork | CompletionReplay:
    with SessionLocal() as db:
        upload = get_owned_upload(db, upload_id, user)
        if idempotency_key:
            existing = db.get(CompleteRequestIdempotency, idempotency_key)
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise HTTPException(status_code=409, detail="idempotency key reused with different complete payload")
                return CompletionReplay(CompleteUploadResponse(upload_id=upload_id, status=upload.status), False)

        status = upload.status
        if status in (UploadStatus.completed.value, UploadStatus.verifying.value):
            if idempotency_key:
                _record_complete_key(db, idempotency_key, upload_id, fingerprint)
                db.commit()
            return CompletionReplay(CompleteUploadResponse(upload_id=upload_id, status=status), True)

        claimable = {UploadStatus.in_progress}
        if upload.total_chunks == 0:
            claimable.add(UploadStatus.initiated)  # an empty file never receives a chunk
        if status == UploadStatus.completing.value:
            lease_cutoff = utc_now() - timedelta(seconds=settings.completion_lease_seconds)
            if not transition(
                db, upload_id, {UploadStatus.completing}, UploadStatus.completing, only_if_updated_before=lease_cutoff
            ):
                raise HTTPException(status_code=409, detail="completion in progress")
        elif status == UploadStatus.initiated.value and UploadStatus.initiated not in claimable:
            raise HTTPException(status_code=409, detail="cannot complete upload from INITIATED state")
        elif UploadStatus(status) in claimable:
            if not transition(db, upload_id, claimable, UploadStatus.completing):
                raise HTTPException(status_code=409, detail="completion in progress")
        else:
            raise HTTPException(status_code=409, detail="cannot complete upload from current state")

        # The claim's UPDATE holds the upload row lock until commit, so chunk records racing with
        # this request either committed before (and are read here) or will see COMPLETING and fail.
        chunks = list(
            db.scalars(
                select(Chunk)
                .where(Chunk.upload_id == upload_id, Chunk.status == ChunkStatus.uploaded.value)
                .order_by(Chunk.chunk_index)
            )
        )
        if len(chunks) != upload.total_chunks:
            raise HTTPException(status_code=409, detail="cannot complete upload, missing chunks")
        stored_bytes = sum(chunk.size_bytes for chunk in chunks)
        if stored_bytes != upload.file_size:
            raise HTTPException(
                status_code=409, detail=f"chunk sizes add up to {stored_bytes} bytes, expected {upload.file_size}"
            )
        multipart_upload_id = (
            upload.multipart_upload_id if settings.storage_backend.lower() in ("s3", "r2") else None
        )
        if multipart_upload_id and any(not chunk.s3_etag for chunk in chunks):
            raise HTTPException(status_code=409, detail="cannot complete upload, missing S3 part etag")
        work = CompletionWork(
            upload_id=upload_id,
            file_checksum_sha256=upload.file_checksum_sha256,
            multipart_upload_id=multipart_upload_id,
            chunk_keys=[chunk.s3_key for chunk in chunks],
            parts=[_part(chunk) for chunk in chunks],
        )
        db.commit()  # nothing below touches ORM objects, so no connection is reopened
    return work


def _part(chunk: Chunk) -> dict:
    part = {"PartNumber": chunk.chunk_index + 1, "ETag": chunk.s3_etag}
    if settings.s3_part_checksums and chunk.chunk_checksum_sha256:
        part["ChecksumSHA256"] = base64.b64encode(bytes.fromhex(chunk.chunk_checksum_sha256)).decode("ascii")
    return part


def _assembled(upload_id: str) -> bool:
    try:
        return storage.object_exists(storage.assembled_key(upload_id))
    except Exception:
        return False


def run_storage_work(work: CompletionWork) -> None:
    """Complete the multipart upload, if any. No DB connection is held. Whole-file checksum
    verification is asynchronous (app/finalizer.py), so it is not done here."""
    if work.multipart_upload_id:
        try:
            with_storage_retries(
                lambda: storage.complete_upload(
                    upload_id=work.upload_id, multipart_upload_id=work.multipart_upload_id, parts=work.parts
                ),
                "multipart completion",
            )
        except HTTPException:
            # A previous attempt may have completed it and died before recording COMPLETED; providers
            # then report different errors (NoSuchUpload on AWS, InvalidPart elsewhere), so decide by
            # whether the assembled object exists, not by the error code.
            if not _assembled(work.upload_id):
                raise


def release_claim(upload_id: str) -> None:
    with SessionLocal() as db:
        transition(db, upload_id, {UploadStatus.completing}, UploadStatus.in_progress)
        db.commit()


def finish_completion(upload_id: str, idempotency_key: str | None, fingerprint: str, verify: bool) -> str:
    """COMPLETING to COMPLETED, or to VERIFYING when a whole-file checksum must be checked first.
    For VERIFYING, the job is enqueued after the commit; if the enqueue fails, the verification
    sweep (app/finalizer.py) re-enqueues it after a lease."""
    target = UploadStatus.verifying if verify else UploadStatus.completed
    with SessionLocal() as db:
        finished = transition(db, upload_id, {UploadStatus.completing}, target)
        if idempotency_key:
            _record_complete_key(db, idempotency_key, upload_id, fingerprint)
        db.commit()
    if finished:
        if verify:
            try:
                job_queue.enqueue(FinalizeJob(upload_id))
            except Exception as exc:
                log_event({"event": "verification_enqueue_error", "upload_id": upload_id, "detail": str(exc)})
        return target.value
    with SessionLocal() as db:
        status = db.scalar(select(Upload.status).where(Upload.id == upload_id))
    if status in (UploadStatus.completed.value, UploadStatus.verifying.value):  # a lease-holder finished it first
        return status
    raise HTTPException(status_code=409, detail=f"upload changed to {status} during completion")


@dataclass(frozen=True)
class AbortWork:
    upload_id: str
    multipart_upload_id: str | None
    chunk_keys: list[str]


def begin_abort(upload_id: str, user: AuthUser) -> tuple[AbortWork | None, str | None]:
    """Mark the upload ABORTED. Returns the storage cleanup to do (None if it was already aborted)
    and the stored failure reason."""
    with SessionLocal() as db:
        upload = get_owned_upload(db, upload_id, user)
        status = upload.status
        if status == UploadStatus.aborted.value:
            return None, upload.failure_reason
        abortable = {UploadStatus.initiated, UploadStatus.in_progress, UploadStatus.failed}
        if UploadStatus(status) not in abortable:
            raise HTTPException(status_code=409, detail=f"cannot abort an upload that is {status}")
        work = AbortWork(
            upload_id=upload_id,
            multipart_upload_id=upload.multipart_upload_id,
            chunk_keys=list(db.scalars(select(Chunk.s3_key).where(Chunk.upload_id == upload_id))),
        )
        if not transition(db, upload_id, abortable, UploadStatus.aborted, failure_reason=ABORT_REASON):
            raise HTTPException(status_code=409, detail="upload changed state, retry the abort")
        db.commit()
    return work, ABORT_REASON


def discard_storage(work: AbortWork) -> None:
    """Best effort: anything left behind is removed later by cleanup."""
    try:
        storage.abort_upload(work.upload_id, work.multipart_upload_id)
    except Exception as exc:
        log_event({"event": "abort_cleanup_error", "upload_id": work.upload_id, "detail": str(exc)})
    for key in sorted(set(work.chunk_keys)):
        try:
            storage.delete_key(key)
        except Exception as exc:
            log_event({"event": "abort_cleanup_error", "upload_id": work.upload_id, "key": key, "detail": str(exc)})
