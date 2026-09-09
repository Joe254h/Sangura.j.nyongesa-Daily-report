"""Statistical helpers used by the regime filter and by trade bookkeeping."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:  # pragma: no cover - typing only, see the note in core/models.py
    from fxbot.core.models import Position


def percentile_rank(x: float, window: npt.ArrayLike) -> float:
    """Return the fraction of ``window`` strictly less than ``x``, in ``[0, 1]``.

    Returns NaN if ``window`` contains fewer than its nominal length of finite values.
    **Never returns 0.0 on an unfilled window** -- that would read as "lowest volatility
    ever recorded" and gate the strategy wrongly for the whole warmup, which is the exact
    failure §7.2 calls out.

    Args:
        x: The value to rank.
        window: The trailing sample to rank it against.

    Returns:
        The rank in ``[0, 1]``, or NaN when the window is not fully populated or ``x``
        is not finite.
    """
    arr = np.asarray(window, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError("percentile_rank() expects a one-dimensional window")
    if arr.size == 0 or not np.isfinite(x):
        return float("nan")
    if int(np.count_nonzero(np.isfinite(arr))) < arr.size:
        return float("nan")
    return float(np.count_nonzero(arr < x) / arr.size)


def r_multiple(pos: Position, price: float) -> float:
    """Return the position's R multiple at ``price``.

    ``(price - entry) / (entry - initial_stop)`` for a BUY, sign-flipped for a SELL.
    Always measured against ``initial_stop`` and the original entry, never the current
    trailing stop -- rewriting the denominator as the stop moves is what makes every
    trade look like it exited at 0R (§7.4).

    Args:
        pos: The open position.
        price: The price to evaluate at -- the close of the last closed bar, never a
            bar extreme and never a live tick (§7.4).

    Returns:
        The R multiple, or 0.0 if the initial risk was zero.
    """
    risk = pos.entry_price - pos.initial_stop
    if pos.side == "SELL":
        risk = -risk
    if risk == 0.0 or not np.isfinite(risk):
        return 0.0
    move = price - pos.entry_price
    if pos.side == "SELL":
        move = -move
    return float(move / risk)
