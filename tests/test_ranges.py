import pytest

from app.download import content_disposition
from app.ranges import ByteRange, RangeNotSatisfiable, parse_range


@pytest.mark.parametrize(
    "header, expected",
    [
        (None, None),
        ("", None),
        ("bytes=0-4", ByteRange(0, 4)),
        ("bytes=-3", ByteRange(7, 9)),  # suffix: the last 3 bytes
        ("bytes=-30", ByteRange(0, 9)),  # suffix longer than the file: the whole file
        ("bytes=5-999", ByteRange(5, 9)),  # end past the file is clamped
        ("bytes=7-", ByteRange(7, 9)),  # open-ended
        ("bytes=9-9", ByteRange(9, 9)),
        ("BYTES=0-0", ByteRange(0, 0)),  # the unit is case-insensitive
        ("bytes=abc-", None),  # malformed: ignored, full body
        ("bytes=0-1,4-5", None),  # multiple ranges: ignored, full body
        ("items=0-1", None),  # unknown unit: ignored
        ("bytes=5-2", None),  # last before first: invalid syntax, ignored
        ("bytes=-", None),
        ("bytes= 0-4", None),
    ],
)
def test_parse_range_table(header, expected) -> None:
    assert parse_range(header, 10) == expected


@pytest.mark.parametrize("header, size", [("bytes=10-", 10), ("bytes=10-20", 10), ("bytes=-0", 10), ("bytes=0-", 0), ("bytes=-5", 0)])
def test_unsatisfiable_ranges_raise(header, size) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_range(header, size)


def test_content_disposition_ascii_name() -> None:
    assert content_disposition("sample.bin") == "attachment; filename=\"sample.bin\"; filename*=UTF-8''sample.bin"


def test_content_disposition_non_ascii_and_unsafe_characters() -> None:
    value = content_disposition('রিপোর্ট "q"\\\r\n.txt')
    fallback = value.split('filename="')[1].split('"')[0]
    assert fallback == "_______ _q____.txt"  # 7 Bengali code points, quotes, backslash, CR, LF replaced
    assert "filename*=UTF-8''%E0%A6%B0" in value
    value.encode("latin-1")  # must be a valid HTTP header value
