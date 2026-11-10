"""RollingWindow: counts, totals and percentiles over the last window_s seconds."""

import pytest

from app.rolling import RollingWindow


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_rolling_window_expires_old_samples() -> None:
    clock = FakeClock()
    window = RollingWindow(window_s=300, clock=clock)
    window.record(10)
    clock.now += 200
    window.record(20)
    assert window.count() == 2
    assert window.total() == 30

    clock.now += 101  # the first sample is now 301 s old
    assert window.count() == 1
    assert window.total() == 20

    clock.now += 300
    assert window.count() == 0
    assert window.total() == 0
    assert window.percentile(50) is None


def test_percentiles_use_nearest_rank() -> None:
    window = RollingWindow(window_s=60, clock=FakeClock())
    for value in range(1, 101):  # 1 to 100
        window.record(value)
    assert window.percentile(50) == 50
    assert window.percentile(95) == 95
    assert window.percentile(100) == 100
    assert window.percentile(0) == 1


def test_percentile_rejects_out_of_range() -> None:
    window = RollingWindow(window_s=60, clock=FakeClock())
    window.record(1)
    with pytest.raises(ValueError, match="between 0 and 100"):
        window.percentile(101)


def test_memory_is_bounded() -> None:
    window = RollingWindow(window_s=60, clock=FakeClock(), max_samples=3)
    for value in (1, 2, 3, 4):
        window.record(value)
    assert window.count() == 3  # the oldest sample made room
    assert window.total() == 9
