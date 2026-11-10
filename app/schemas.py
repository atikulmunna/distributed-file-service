import unicodedata
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, field_validator


def _as_utc(value: datetime) -> datetime:
    """The app stores UTC. SQLite returns it without a time zone, Postgres with one: always emit UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class InitUploadRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    file_size: int = Field(ge=0)  # 0 is an empty file with no chunks
    chunk_size: int | None = Field(default=None, gt=0)
    file_checksum_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("file_name")
    @classmethod
    def _no_control_characters(cls, value: str) -> str:
        if any(unicodedata.category(ch) == "Cc" for ch in value):
            raise ValueError("file_name must not contain control characters")
        return value


class InitUploadResponse(BaseModel):
    upload_id: str
    chunk_size: int
    total_chunks: int
    status: str


class UploadChunkResponse(BaseModel):
    upload_id: str
    chunk_index: int
    status: str


class CompleteUploadResponse(BaseModel):
    upload_id: str
    status: str


class UploadStatusResponse(BaseModel):
    upload_id: str
    status: str
    failure_reason: str | None = None


class UploadSummary(BaseModel):
    upload_id: str
    file_name: str
    file_size: int
    total_chunks: int
    uploaded_chunks: int
    status: str
    created_at: UtcDatetime
    last_activity_at: UtcDatetime


class UploadListResponse(BaseModel):
    items: list[UploadSummary]
    next_cursor: str | None = Field(description="Pass as cursor= for the next page; null on the last page")


class UploadDetail(BaseModel):
    upload_id: str
    file_name: str
    file_size: int
    chunk_size: int
    total_chunks: int
    status: str
    uploaded_chunks: int
    uploaded_bytes: int
    file_checksum_sha256: str | None
    checksum_verified: bool | None = Field(
        description="true once the whole-file checksum matched, false if it did not, null if none was "
        "declared or verification has not finished"
    )
    failure_reason: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    last_activity_at: UtcDatetime


class ChunkInfo(BaseModel):
    index: int
    size_bytes: int
    sha256: str | None
    status: str
    retry_count: int
    updated_at: UtcDatetime


class ChunkListResponse(BaseModel):
    items: list[ChunkInfo]
    total: int = Field(description="Number of recorded chunks")


class LimitsResponse(BaseModel):
    max_file_size_bytes: int
    min_chunk_size_bytes: int
    max_chunk_size_bytes: int
    default_chunk_size_bytes: int
    max_total_chunks: int
    max_inflight_chunks_per_upload: int
    storage_backend_requires_min_part: bool = Field(
        description="Every chunk but the last must be at least min_part_size_bytes (S3 multipart rule)"
    )
    min_part_size_bytes: int | None


class ServiceStats(BaseModel):
    """This instance only (see instance_id); other instances keep their own numbers."""

    instance_id: str
    app_version: str
    uptime_s: float
    inflight_chunks: int
    inflight_limit: int
    queued_chunks: int
    queue_limit: int
    worker_threads: int
    throttled_last_5m: int
    chunks_last_5m: int
    bytes_last_5m: int
    storage_write_ms_p50: float | None
    storage_write_ms_p95: float | None
    window_s: int


class DownloadUrlResponse(BaseModel):
    url: str
    expires_at: UtcDatetime


class MissingChunksResponse(BaseModel):
    upload_id: str
    missing_chunk_indexes: list[int]
    status: str


class ErrorResponse(BaseModel):
    """Every error body. 5xx bodies carry only detail, error_code and request_id."""

    detail: str
    error_code: str
    request_id: str | None = None
    upload_id: str | None = None
    trace_id: str | None = None


class FieldError(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ValidationErrorResponse(ErrorResponse):
    errors: list[FieldError]
