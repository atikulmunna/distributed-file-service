import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class UploadStatus(str, enum.Enum):
    initiated = "INITIATED"
    in_progress = "IN_PROGRESS"
    completed = "COMPLETED"
    failed = "FAILED"
    aborted = "ABORTED"


class ChunkStatus(str, enum.Enum):
    pending = "PENDING"
    uploaded = "UPLOADED"
    failed = "FAILED"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, default="legacy")
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    file_checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=UploadStatus.initiated.value)
    multipart_upload_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    chunks: Mapped[list["Chunk"]] = relationship(back_populates="upload", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("upload_id", "chunk_index", name="uq_upload_chunk_index"),
        Index("idx_chunks_upload_status", "upload_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upload_id: Mapped[str] = mapped_column(String(36), ForeignKey("uploads.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    s3_etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ChunkStatus.pending.value)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    upload: Mapped[Upload] = relationship(back_populates="chunks")


class InitRequestIdempotency(Base):
    __tablename__ = "init_request_idempotency"

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    upload_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("uploads.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ChunkRequestIdempotency(Base):
    __tablename__ = "chunk_request_idempotency"
    __table_args__ = (
        UniqueConstraint("upload_id", "chunk_index", "idempotency_key", name="uq_chunk_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upload_id: Mapped[str] = mapped_column(String(36), ForeignKey("uploads.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class CompleteRequestIdempotency(Base):
    __tablename__ = "complete_request_idempotency"

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    upload_id: Mapped[str] = mapped_column(String(36), ForeignKey("uploads.id", ondelete="CASCADE"), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
