"""How a file of a given size splits into fixed-size chunks (the last one may be shorter)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkLayout:
    file_size: int
    chunk_size: int

    def __post_init__(self) -> None:
        if self.file_size < 0:
            raise ValueError("file_size must be >= 0")
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be > 0")

    @property
    def total_chunks(self) -> int:
        return -(-self.file_size // self.chunk_size)  # ceiling division; 0 for an empty file

    def _check_index(self, index: int) -> None:
        if not 0 <= index < self.total_chunks:
            raise IndexError(f"chunk index {index} outside 0 to {self.total_chunks - 1}")

    def expected_size(self, index: int) -> int:
        self._check_index(index)
        return min(self.chunk_size, self.file_size - index * self.chunk_size)

    def chunk_start(self, index: int) -> int:
        self._check_index(index)
        return index * self.chunk_size

    def chunk_for_offset(self, offset: int) -> int:
        if not 0 <= offset < self.file_size:
            raise IndexError(f"offset {offset} outside the file (size {self.file_size})")
        return offset // self.chunk_size
