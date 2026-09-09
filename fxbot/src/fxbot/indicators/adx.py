"""Wilder's Average Directional Index."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from fxbot.indicators.atr import true_range, wilder_smooth


def directional_movement(
    high: npt.NDArray[np.float64], low: npt.NDArray[np.float64]
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Return ``(+DM, -DM)`` per Wilder, same length as the input.

    Only one of the two can be non-zero on any bar: an inside bar produces neither, and
    an outside bar credits whichever move was larger.
    """
    plus = np.zeros(high.shape, dtype=np.float64)
    minus = np.zeros(high.shape, dtype=np.float64)
    if high.size < 2:
        return plus, minus
    up = high[1:] - high[:-1]
    down = low[:-1] - low[1:]
    plus[1:] = np.where((up > down) & (up > 0.0), up, 0.0)
    minus[1:] = np.where((down > up) & (down > 0.0), down, 0.0)
    return plus, minus


def adx(
    high: npt.ArrayLike, low: npt.ArrayLike, close: npt.ArrayLike, period: int = 14
) -> npt.NDArray[np.float64]:
    """Return Wilder's ADX, NaN-padded to the input length.

    ``+DI = 100 * smooth(+DM) / smooth(TR)``, likewise ``-DI``;
    ``DX = 100 * |+DI - -DI| / (+DI + -DI)``; ``ADX = wilder_smooth(DX, period)``.

    The first finite value therefore lands at index ``2 * period - 1``: one ``period`` to
    smooth the directional movement and another to smooth DX. Anything that reports an
    ADX earlier than that is not Wilder's ADX.

    Args:
        high: Bar highs.
        low: Bar lows.
        close: Bar closes.
        period: Wilder period, default 14.

    Returns:
        A float array of the same length; the first finite value lands at index
        ``2 * period - 1``.
    """
    h = np.asarray(high, dtype=np.float64)
    low_arr = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    if not (h.size == low_arr.size == c.size):
        raise ValueError("high, low and close must be the same length")
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    n = h.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < 2 * period:
        return out

    plus_dm, minus_dm = directional_movement(h, low_arr)
    tr = true_range(h, low_arr, c)

    # Wilder's smoothing of DM and TR starts on the same bar. Bar 0 has no directional
    # movement by definition, so the sums are seeded over bars 1..period.
    sm_tr = wilder_smooth(tr[1:], period)
    sm_plus = wilder_smooth(plus_dm[1:], period)
    sm_minus = wilder_smooth(minus_dm[1:], period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * np.divide(sm_plus, sm_tr, out=np.full_like(sm_tr, np.nan),
                                    where=sm_tr > 0.0)
        minus_di = 100.0 * np.divide(sm_minus, sm_tr, out=np.full_like(sm_tr, np.nan),
                                     where=sm_tr > 0.0)
        di_sum = plus_di + minus_di
        dx = 100.0 * np.divide(np.abs(plus_di - minus_di), di_sum,
                               out=np.full_like(di_sum, np.nan), where=di_sum > 0.0)

    # A flat market can give +DI == -DI == 0 and an undefined DX. Wilder's own treatment
    # is to carry a zero rather than break the smoothing chain; NaN here would poison
    # every later value and gate the strategy off permanently on a quiet symbol.
    dx = np.where(np.isnan(dx) & np.isfinite(sm_tr), 0.0, dx)

    smoothed = wilder_smooth(dx, period)
    out[1:] = smoothed
    return out
