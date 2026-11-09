"""HTTP Range header parsing (RFC 9110, section 14) for a single byte range.

Malformed, multi-range, or non-bytes headers are ignored (the caller serves the full body with 200),
which RFC 9110 allows. Only a syntactically valid range that cannot be satisfied raises, so the caller
answers 416 with Content-Range: bytes */<size>.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_RANGE_SPEC = re.compile(r"^(\d*)-(\d*)$")


class RangeNotSatisfiable(Exception):
    pass


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int  # inclusive

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_range(header: str | None, size: int) -> ByteRange | None:
    if not header:
        return None
    unit, separator, spec = header.partition("=")
    if not separator or unit.strip().lower() != "bytes" or "," in spec:
        return None
    match = _RANGE_SPEC.match(spec)
    if not match:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:  # suffix range: the last N bytes
        suffix = int(last)
        if suffix == 0 or size == 0:
            raise RangeNotSatisfiable
        return ByteRange(max(0, size - suffix), size - 1)
    start = int(first)
    if last and int(last) < start:
        return None  # invalid syntax, not unsatisfiable
    if start >= size:
        raise RangeNotSatisfiable
    end = int(last) if last else size - 1
    return ByteRange(start, min(end, size - 1))
