"""Planning and streaming downloads.

prepare_download reads everything it needs in one short DB session (the session is closed before any
byte is streamed). It maps the requested byte range to pieces of stored objects: the assembled object
for an S3 multipart upload, or the chunk objects that cover the range otherwise. iter_pieces then
streams those pieces: by default one streaming ranged read per piece; with download_prefetch above 1,
as segment-sized ranged reads with that many in flight on a dedicated thread pool, yielded in order.
"""

from __future__ import annotations

import itertools
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import select

from app.auth import AuthUser
from app.config import settings
from app.db import SessionLocal
from app.layout import ChunkLayout
from app.models import Chunk, Upload, UploadStatus
from app.ranges import ByteRange, RangeNotSatisfiable, parse_range
from app.storage import storage
from app.storage_policy import with_storage_retries
from app.upload_access import get_owned_upload

_download_executor = ThreadPoolExecutor(
    max_workers=max(8, settings.download_prefetch * 4), thread_name_prefix="download"
)


@dataclass(frozen=True)
class Piece:
    key: str
    start: int  # inclusive offsets within the stored object
    end: int


@dataclass(frozen=True)
class DownloadPlan:
    upload_id: str
    file_name: str
    file_size: int
    byte_range: ByteRange | None  # None: the whole file
    pieces: list[Piece] = field(default_factory=list)

    @property
    def length(self) -> int:
        return self.byte_range.length if self.byte_range else self.file_size


def object_pieces(upload: Upload, chunks: list[Chunk], layout: ChunkLayout, start: int, end: int) -> list[Piece]:
    """Map file bytes start..end (inclusive) to ranges of stored objects."""
    if end < start:
        return []
    if upload.multipart_upload_id and settings.storage_backend.lower() in ("s3", "r2"):
        return [Piece(storage.assembled_key(upload.id), start, end)]
    by_index = {chunk.chunk_index: chunk for chunk in chunks}
    pieces = []
    for index in range(layout.chunk_for_offset(start), layout.chunk_for_offset(end) + 1):
        chunk_start = layout.chunk_start(index)
        chunk_end = chunk_start + layout.expected_size(index) - 1
        pieces.append(Piece(by_index[index].s3_key, max(start, chunk_start) - chunk_start, min(end, chunk_end) - chunk_start))
    return pieces


def prepare_download(upload_id: str, user: AuthUser, range_header: str | None) -> DownloadPlan:
    with SessionLocal() as db:
        upload = get_owned_upload(db, upload_id, user)
        if upload.status == UploadStatus.verifying.value:
            raise HTTPException(status_code=409, detail="verification in progress")
        if upload.status != UploadStatus.completed.value:
            raise HTTPException(status_code=409, detail="upload is not completed")
        chunks = list(db.scalars(select(Chunk).where(Chunk.upload_id == upload_id).order_by(Chunk.chunk_index)))
        if len(chunks) != upload.total_chunks:
            raise HTTPException(status_code=500, detail="upload metadata is inconsistent")
        try:
            byte_range = parse_range(range_header, upload.file_size)
        except RangeNotSatisfiable:
            raise HTTPException(
                status_code=416,
                detail="range not satisfiable",
                headers={"Content-Range": f"bytes */{upload.file_size}"},
            ) from None
        start, end = (byte_range.start, byte_range.end) if byte_range else (0, upload.file_size - 1)
        return DownloadPlan(
            upload_id=upload.id,
            file_name=upload.file_name,
            file_size=upload.file_size,
            byte_range=byte_range,
            pieces=object_pieces(upload, chunks, ChunkLayout(upload.file_size, upload.chunk_size), start, end),
        )


def _segments(pieces: list[Piece], segment_bytes: int) -> Iterator[tuple[str, int, int]]:
    for piece in pieces:
        start = piece.start
        while start <= piece.end:
            end = min(start + segment_bytes - 1, piece.end)
            yield piece.key, start, end
            start = end + 1


def _read_segment(key: str, start: int, end: int) -> bytes:
    return with_storage_retries(lambda: storage.read_range(key, start, end), "download")


def _stream_piece(piece: Piece, block_size: int) -> Iterator[bytes]:
    """One streaming ranged read. Opening it (up to the first block) follows the storage retry policy."""

    def open_stream() -> tuple[bytes, Iterator[bytes]]:
        blocks = storage.iter_range(piece.key, piece.start, piece.end, block_size)
        return next(blocks, b""), blocks

    first, rest = with_storage_retries(open_stream, "download")
    if first:
        yield first
    yield from rest


def iter_pieces(pieces: list[Piece], segment_bytes: int, prefetch: int) -> Iterator[bytes]:
    """Yield the pieces' bytes in order.

    prefetch <= 1: one streaming ranged read per piece, in blocks of segment_bytes.
    prefetch > 1: segment-sized ranged reads with up to `prefetch` in flight.

    Memory is bounded by about (prefetch + 1) segments. If the consumer stops early (client gone),
    reads that have not started are cancelled.
    """
    if prefetch <= 1:
        for piece in pieces:
            yield from _stream_piece(piece, segment_bytes)
        return
    segments = _segments(pieces, segment_bytes)
    pending: deque[Future] = deque(
        _download_executor.submit(_read_segment, *segment) for segment in itertools.islice(segments, prefetch)
    )
    try:
        while pending:
            data = pending.popleft().result()
            following = next(segments, None)
            if following is not None:
                pending.append(_download_executor.submit(_read_segment, *following))
            yield data
    finally:
        for future in pending:
            future.cancel()


def content_disposition(file_name: str) -> str:
    """attachment header with an ASCII fallback name and the exact name in RFC 5987 form."""
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in file_name)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(file_name, safe='')}"
