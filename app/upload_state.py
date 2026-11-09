"""Upload status transitions as single conditional UPDATEs, so concurrent requests cannot both win."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Upload, UploadStatus, utc_now


def transition(
    db: Session,
    upload_id: str,
    allowed_from: set[UploadStatus],
    to: UploadStatus,
    failure_reason: str | None = None,
    only_if_updated_before: datetime | None = None,
) -> bool:
    """Move the upload to `to` if its status is in `allowed_from` (and, when given, it was last
    updated before `only_if_updated_before`). Returns True when this call made the change.
    Runs in the caller's transaction; the caller commits."""
    statement = (
        update(Upload)
        .where(Upload.id == upload_id, Upload.status.in_([status.value for status in allowed_from]))
        .values(status=to.value, updated_at=utc_now(), failure_reason=failure_reason)
        # Callers never rely on ORM objects after a transition, so skip in-Python re-evaluation of
        # the WHERE clause (it would compare SQLite's naive timestamps with an aware cutoff).
        .execution_options(synchronize_session=False)
    )
    if only_if_updated_before is not None:
        statement = statement.where(Upload.updated_at < only_if_updated_before)
    return db.execute(statement).rowcount == 1
