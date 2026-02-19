from pydantic import BaseModel, Field


class InitUploadRequest(BaseModel):
    file_name: str = Field(min_length=1)
    file_size: int = Field(gt=0)
    chunk_size: int | None = Field(default=None, gt=0)


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


class MissingChunksResponse(BaseModel):
    upload_id: str
    missing_chunk_indexes: list[int]
    status: str


class ErrorResponse(BaseModel):
    detail: str
    error_code: str
    request_id: str | None = None
    upload_id: str | None = None
