import unicodedata

from pydantic import BaseModel, Field, field_validator


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


class MissingChunksResponse(BaseModel):
    upload_id: str
    missing_chunk_indexes: list[int]
    status: str


class ErrorResponse(BaseModel):
    detail: str
    error_code: str
    request_id: str | None = None
    upload_id: str | None = None
    trace_id: str | None = None
