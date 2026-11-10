"""Recent activity of this instance, for GET /v1/stats: what happened in the last few minutes.

Prometheus counters and histograms (app/metrics.py) are cumulative; these windows answer "what
happened in the last window_s seconds" directly, without a metrics server.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable

STATS_WINDOW_S = 300


class RollingWindow:
    """Samples of the last window_s seconds. Thread-safe; at most max_samples are kept (the oldest
    are dropped first), so memory stays bounded under any load."""

    def __init__(self, window_s: int, clock: Callable[[], float] = time.monotonic, max_samples: int = 100_000) -> None:
        self.window_s = window_s
        self._clock = clock
        self._samples: deque[tuple[float, float]] = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def record(self, value: float = 1.0) -> None:
        with self._lock:
            self._samples.append((self._clock(), value))

    def _current(self) -> list[float]:
        cutoff = self._clock() - self.window_s
        with self._lock:
            while self._samples and self._samples[0][0] <= cutoff:
                self._samples.popleft()
            return [value for _, value in self._samples]

    def count(self) -> int:
        return len(self._current())

    def total(self) -> float:
        return sum(self._current())

    def percentile(self, p: float) -> float | None:
        """Nearest-rank percentile of the current samples; None when there are none."""
        if not 0 <= p <= 100:
            raise ValueError("p must be between 0 and 100")
        values = sorted(self._current())
        if not values:
            return None
        rank = max(1, math.ceil(p / 100 * len(values)))
        return values[rank - 1]

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()


# This instance's activity, shown by GET /v1/stats.
chunk_writes = RollingWindow(STATS_WINDOW_S)  # one sample per recorded chunk; the value is its size in bytes
storage_write_ms = RollingWindow(STATS_WINDOW_S)  # storage write latency of each chunk, in milliseconds
throttled = RollingWindow(STATS_WINDOW_S)  # one sample per request answered with 429
