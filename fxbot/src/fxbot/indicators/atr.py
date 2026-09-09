"""True range and Wilder's Average True Range."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt


def true_range(
    high: npt.ArrayLike, low: npt.ArrayLike, close: npt.ArrayLike
) -> npt.NDArray[np.float64]:
    """Return the true range series, same length as the input.

    ``TR[i] = max(h[i] - l[i], |h[i] - c[i-1]|, |l[i] - c[i-1]|)``. The first bar has no
    previous close, so ``TR[0] = h[0] - l[0]``.

    Args:
        high: Bar highs.
        low: Bar lows.
        close: Bar closes.

    Returns:
        A float array of true ranges.

    Raises:
        ValueError: If the three inputs differ in length or are not 1-D.
    """
    h = np.asarray(high, dtype=np.float64)
    low_arr = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    if not (h.ndim == low_arr.ndim == c.ndim == 1):
        raise ValueError("true_range() expects one-dimensional series")
    if not (h.size == low_arr.size == c.size):
        raise ValueError("high, low and close must be the same length")

    tr = np.empty(h.shape, dtype=np.float64)
    if h.size == 0:
        return tr
    tr[0] = h[0] - low_arr[0]
    if h.size > 1:
        prev_close = c[:-1]
        tr[1:] = np.maximum.reduce([
            h[1:] - low_arr[1:],
            np.abs(h[1:] - prev_close),
            np.abs(low_arr[1:] - prev_close),
        ])
    return tr


def wilder_smooth(values: npt.NDArray[np.float64], period: int) -> npt.NDArray[np.float64]:
    """Apply Wilder's smoothing to ``values``, NaN-padded to the input length.

    Wilder's smoothing is **not** an SMA and not a standard EMA: it seeds on the simple
    mean of the first ``period`` values and then applies
    ``s[i] = (s[i-1] * (period - 1) + v[i]) / period``, i.e. ``alpha = 1 / period``.

    Args:
        values: The series to smooth.
        period: Wilder period, ``>= 1``.

    Returns:
        A float array of the same length; the first ``period - 1`` entries are NaN.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    out = np.full(values.shape, np.nan, dtype=np.float64)
    n = values.size
    if n < period:
        return out

    # Skip any NaN pad the caller's own upstream indicator left in front: ADX smooths DX,
    # which is itself undefined for the first `period - 1` bars. Bailing on the leading
    # NaNs instead of stepping over them would make ADX all-NaN forever.
    start = _first_finite_window(values, period)
    if start is None:
        return out

    prev = float(values[start:start + period].mean())
    out[start + period - 1] = prev
    for i in range(start + period, n):
        value = values[i]
        if not np.isfinite(value):
            # A NaN appearing after warmup is a data fault, not a warmup artefact. Stop
            # rather than carrying the last good value forward: §0.7 says fail closed, and
            # every consumer already treats NaN as "reject".
            return out
        prev = (prev * (period - 1) + value) / period
        out[i] = prev
    return out


def _first_finite_window(values: npt.NDArray[np.float64], period: int) -> int | None:
    """Return the first index whose next ``period`` values are all finite, else None."""
    finite = np.isfinite(values)
    n = finite.size
    run = 0
    for i in range(n):
        run = run + 1 if finite[i] else 0
        if run >= period:
            return i - period + 1
    return None


def atr(
    high: npt.ArrayLike, low: npt.ArrayLike, close: npt.ArrayLike, period: int = 14
) -> npt.NDArray[np.float64]:
    """Return Wilder's ATR, NaN-padded to the input length.

    Args:
        high: Bar highs.
        low: Bar lows.
        close: Bar closes.
        period: Wilder period, default 14.

    Returns:
        A float array of the same length; the first ``period - 1`` entries are NaN.
    """
    return wilder_smooth(true_range(high, low, close), period)
