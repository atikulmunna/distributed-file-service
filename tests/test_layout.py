import pytest

from app.layout import ChunkLayout


def test_layout_sizes() -> None:
    even = ChunkLayout(10, 5)
    assert even.total_chunks == 2
    assert [even.expected_size(i) for i in range(even.total_chunks)] == [5, 5]

    ragged = ChunkLayout(11, 5)
    assert ragged.total_chunks == 3
    assert [ragged.expected_size(i) for i in range(3)] == [5, 5, 1]
    assert ragged.chunk_start(2) == 10
    assert ragged.chunk_for_offset(10) == 2
    assert ragged.chunk_for_offset(4) == 0

    assert ChunkLayout(0, 5).total_chunks == 0
    assert ChunkLayout(3, 8).expected_size(0) == 3


def test_layout_rejects_out_of_range_indexes_and_offsets() -> None:
    layout = ChunkLayout(11, 5)
    for bad_index in (-1, 3):
        with pytest.raises(IndexError):
            layout.expected_size(bad_index)
        with pytest.raises(IndexError):
            layout.chunk_start(bad_index)
    for bad_offset in (-1, 11):
        with pytest.raises(IndexError):
            layout.chunk_for_offset(bad_offset)


def test_layout_rejects_invalid_dimensions() -> None:
    with pytest.raises(ValueError):
        ChunkLayout(-1, 5)
    with pytest.raises(ValueError):
        ChunkLayout(10, 0)
