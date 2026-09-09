"""Donchian channel."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt


def donchian(
    high: npt.ArrayLike, low: npt.ArrayLike, period: int = 20
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Return ``(upper, lower)`` Donchian bands, NaN-padded to the input length.

    ``upper[i]`` is the highest high over ``[i - period + 1, i]`` **inclusive of bar i**.
    The strategy is what excludes the signal bar, by reading ``upper[-2]`` (§7.3 step 5);
    doing the exclusion here as well would shift the channel twice.

    Args:
        high: Bar highs.
        low: Bar lows.
        period: Channel length, default 20.

    Returns:
        Two float arrays of the input length; the first ``period - 1`` entries are NaN.

    Raises:
        ValueError: If the inputs differ in length or ``period`` is not positive.
    """
    h = np.asarray(high, dtype=np.float64)
    low_arr = np.asarray(low, dtype=np.float64)
    if h.size != low_arr.size:
        raise ValueError("high and low must be the same length")
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    n = h.size
    upper = np.full(n, np.nan, dtype=np.float64)
    lower = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return upper, lower

    strides_h = np.lib.stride_tricks.sliding_window_view(h, period)
    strides_l = np.lib.stride_tricks.sliding_window_view(low_arr, period)
    upper[period - 1:] = strides_h.max(axis=1)
    lower[period - 1:] = strides_l.min(axis=1)
    return upper, lower
