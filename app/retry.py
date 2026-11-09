"""Retry with capped exponential backoff and full jitter.

Delay before retry n (0-based) is uniform in [0, min(max_delay_s, base_delay_s * 2**n)]. Full jitter
spreads retries from many clients over the whole window instead of synchronizing them.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def call_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int,
    base_delay_s: float,
    max_delay_s: float,
    is_retryable: Callable[[Exception], bool],
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[Exception], None] | None = None,
    rng: Callable[[], float] = random.random,
) -> T:
    """Call `fn` up to `attempts` times. Non-retryable errors and the last failure propagate."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts - 1 or not is_retryable(exc):
                raise
            if on_retry is not None:
                on_retry(exc)
            sleep(rng() * min(max_delay_s, base_delay_s * 2**attempt))
    raise ValueError("attempts must be at least 1")
