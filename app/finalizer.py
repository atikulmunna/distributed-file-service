"""Verification worker: hashes a completed upload's stored file and moves it from VERIFYING to
COMPLETED (checksum matches) or FAILED (mismatch, or the file could not be read after
finalizer_max_attempts).

Runs inside the API process (finalizer_in_process) or on its own: `python -m app.finalizer`.
Jobs are delivered at least once, so process() is idempotent: anything not in VERIFYING is skipped.
No DB connection is held while the file is read.
"""

from __future__ import annotations

import hashlib
import signal
import sys
import threading
import time
from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.download import iter_pieces, object_pieces
from app.jobs import FinalizeJob, JobQueue, MemoryJobQueue, Reservation, job_queue
from app.layout import ChunkLayout
from app.logging_utils import log_event
from app.metrics import finalize_duration_seconds, finalize_jobs_total
from app.models import CHECKSUM_MISMATCH, Chunk, Upload, UploadStatus, utc_now
from app.upload_state import transition


def _plan(job: FinalizeJob):
    with SessionLocal() as db:
        upload = db.get(Upload, job.upload_id)
        if upload is None or upload.status != UploadStatus.verifying.value:
            return None
        chunks = list(db.scalars(select(Chunk).where(Chunk.upload_id == job.upload_id).order_by(Chunk.chunk_index)))
        layout = ChunkLayout(upload.file_size, upload.chunk_size)
        return upload.file_checksum_sha256, object_pieces(upload, chunks, layout, 0, upload.file_size - 1)


def process(job: FinalizeJob) -> str:
    """Verify one upload. Returns the outcome: completed, failed, or skipped."""
    started = time.perf_counter()
    plan = _plan(job)
    if plan is None:
        finalize_jobs_total.labels(outcome="skipped").inc()
        return "skipped"
    expected, pieces = plan
    digest = hashlib.sha256()
    for block in iter_pieces(pieces, settings.download_segment_bytes, prefetch=1):
        digest.update(block)
    actual = digest.hexdigest()
    with SessionLocal() as db:
        if expected is None or actual == expected:
            transition(db, job.upload_id, {UploadStatus.verifying}, UploadStatus.completed)
            outcome = "completed"
        else:
            reason = f"{CHECKSUM_MISMATCH}: expected {expected}, got {actual}"
            transition(db, job.upload_id, {UploadStatus.verifying}, UploadStatus.failed, failure_reason=reason)
            outcome = "failed"
        db.commit()
    finalize_jobs_total.labels(outcome=outcome).inc()
    finalize_duration_seconds.observe(time.perf_counter() - started)
    log_event(
        {"event": "verification_finished", "upload_id": job.upload_id, "outcome": outcome, "attempt": job.attempt}
    )
    return outcome


def handle(queue: JobQueue, reservation: Reservation, retry_delay_s: float | None = None) -> None:
    """Run one delivery: ack on success; on error release it for a later attempt, or give up and
    mark the upload FAILED after finalizer_max_attempts."""
    job = reservation.job
    try:
        process(job)
        queue.ack(reservation)
    except Exception as exc:  # noqa: BLE001  a failed job is released with backoff and retried
        attempt = job.attempt + 1
        log_event(
            {
                "event": "verification_error",
                "upload_id": job.upload_id,
                "attempt": attempt,
                "error_class": type(exc).__name__,
                "detail": str(exc),
            }
        )
        if attempt >= settings.finalizer_max_attempts:
            with SessionLocal() as db:
                transition(
                    db,
                    job.upload_id,
                    {UploadStatus.verifying},
                    UploadStatus.failed,
                    failure_reason=f"verification failed after {attempt} attempts: {type(exc).__name__}",
                )
                db.commit()
            queue.ack(reservation)
            finalize_jobs_total.labels(outcome="gave_up").inc()
        else:
            delay = retry_delay_s if retry_delay_s is not None else min(60.0, 2.0**attempt)
            queue.release(reservation, delay_s=delay, job=FinalizeJob(job.upload_id, attempt))
            finalize_jobs_total.labels(outcome="retried").inc()


def sweep_lost_jobs(queue: JobQueue) -> int:
    """Re-enqueue uploads stuck in VERIFYING longer than a lease (their job was lost, for example the
    queue was down when it was enqueued). Touches each one so it is not requeued again right away."""
    cutoff = utc_now() - timedelta(seconds=settings.job_lease_seconds)
    with SessionLocal() as db:
        stuck = list(
            db.scalars(
                select(Upload.id).where(Upload.status == UploadStatus.verifying.value, Upload.updated_at < cutoff)
            )
        )
        for upload_id in stuck:
            transition(db, upload_id, {UploadStatus.verifying}, UploadStatus.verifying)
        db.commit()
    for upload_id in stuck:
        queue.enqueue(FinalizeJob(upload_id))
    if stuck:
        log_event({"event": "verification_requeued", "count": len(stuck)})
    return len(stuck)


def _worker_loop(queue: JobQueue, stop: threading.Event, poll_timeout_s: float, sweeps: bool) -> None:
    next_sweep = 0.0
    while not stop.is_set():
        try:
            if sweeps and time.monotonic() >= next_sweep:
                sweep_lost_jobs(queue)
                next_sweep = time.monotonic() + settings.verification_sweep_seconds
            reservation = queue.reserve(timeout_s=poll_timeout_s)
            if reservation is not None:
                handle(queue, reservation)
        except Exception as exc:  # noqa: BLE001  keep the worker alive; the queue redelivers anything unacked
            log_event({"event": "verification_worker_error", "error_class": type(exc).__name__, "detail": str(exc)})
            stop.wait(1)


def start_workers(
    queue: JobQueue, concurrency: int, stop: threading.Event, poll_timeout_s: float
) -> list[threading.Thread]:
    threads = [
        threading.Thread(
            target=_worker_loop, args=(queue, stop, poll_timeout_s, index == 0), name=f"finalizer-{index}", daemon=True
        )
        for index in range(max(1, concurrency))
    ]
    for thread in threads:
        thread.start()
    return threads


def main() -> int:
    if isinstance(job_queue, MemoryJobQueue):
        print("QUEUE_BACKEND=memory only works with finalizer_in_process; use redis or sqs for a separate worker")
        return 2
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    threads = start_workers(job_queue, settings.finalizer_concurrency, stop, settings.queue_poll_timeout_seconds)
    log_event({"event": "finalizer_started", "workers": len(threads), "queue_backend": settings.queue_backend})
    while not stop.is_set():
        stop.wait(1)
    for thread in threads:
        thread.join(timeout=settings.queue_poll_timeout_seconds + 5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
