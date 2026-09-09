"""Exponential moving average."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt


def ema(values: npt.ArrayLike, period: int) -> npt.NDArray[np.float64]:
    """Return the EMA of ``values``, NaN-padded to the input length.

    The series is seeded with the simple mean of the first ``period`` observations -- the
    convention MetaTrader and Backtrader both use -- and then follows
    ``e[i] = a * v[i] + (1 - a) * e[i-1]`` with ``a = 2 / (period + 1)``.

    Seeding matters for parity: an EMA seeded on the first value alone carries a
    transient hundreds of bars long, and two engines that start at different bars would
    then disagree on the confirmation filter for months of backtest.

    Args:
        values: One-dimensional price series.
        period: Lookback length, ``>= 1``.

    Returns:
        A float array of the same length; the first ``period - 1`` entries are NaN.

    Raises:
        ValueError: If ``period`` is not positive or ``values`` is not 1-D.
    """
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError("ema() expects a one-dimensional series")
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    out = np.full(arr.shape, np.nan, dtype=np.float64)
    n = arr.size
    if n < period:
        return out

    seed = arr[:period]
    if not np.all(np.isfinite(seed)):
        return out

    alpha = 2.0 / (period + 1.0)
    prev = float(seed.mean())
    out[period - 1] = prev
    for i in range(period, n):
        value = arr[i]
        if not np.isfinite(value):
            return out
        prev = alpha * value + (1.0 - alpha) * prev
        out[i] = prev
    return out
