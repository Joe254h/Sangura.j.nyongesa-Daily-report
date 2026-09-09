"""Higher-timeframe bias and H1 regime classification (§7.3 steps 1 and 2). All pure."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from fxbot.config.schema import StrategyParams
from fxbot.core.enums import Bias, Regime
from fxbot.indicators.adx import adx
from fxbot.indicators.atr import atr
from fxbot.indicators.ema import ema
from fxbot.indicators.stats import percentile_rank


def htf_bias(d1: pd.DataFrame, p: StrategyParams) -> Bias:
    """Return the daily directional permission (§7.3 step 1).

    ``LONG_ONLY`` above the D1 EMA plus a dead band, ``SHORT_ONLY`` below it minus the
    band, ``NEUTRAL`` inside. The band exists to stop the bias flipping every day while
    price oscillates around the EMA.

    The frame must contain only **closed** daily bars: during the trading day the last row
    is yesterday's close. Using today's forming daily bar is lookahead and is the most
    common silent bug in multi-timeframe FX systems (§17.2); ``data/resample.py``
    guarantees the frame handed here never includes ``ctx.now``'s own trading day.

    Args:
        d1: Closed daily bars, ascending, with ``high``/``low``/``close`` columns.
        p: Strategy parameters.

    Returns:
        The bias. ``NEUTRAL`` whenever the inputs are not yet finite -- fail closed.
    """
    needed = max(p.d1_ema, p.atr_period) + 1
    if len(d1) < needed:
        return Bias.NEUTRAL

    close = d1["close"].to_numpy(dtype=np.float64)
    d1_ema = ema(close, p.d1_ema)[-1]
    d1_atr = atr(d1["high"].to_numpy(dtype=np.float64),
                 d1["low"].to_numpy(dtype=np.float64),
                 close, p.atr_period)[-1]
    if not (math.isfinite(d1_ema) and math.isfinite(d1_atr)):
        return Bias.NEUTRAL

    band = p.d1_neutral_band_atr * d1_atr
    last = float(close[-1])
    if last > d1_ema + band:
        return Bias.LONG_ONLY
    if last < d1_ema - band:
        return Bias.SHORT_ONLY
    return Bias.NEUTRAL


def classify_regime(h1: pd.DataFrame, p: StrategyParams) -> tuple[Regime, float, float]:
    """Classify the H1 regime (§7.3 step 2).

    ``EXTREME`` when the ATR percentile rank is above the ceiling (news, blowout),
    ``RANGING`` when ADX is below its floor or volatility is in the dead zone,
    ``TRENDING`` otherwise.

    **Ambiguity resolved (§18).** "trailing ``atr_pct_window`` values" does not say
    whether the current bar belongs to its own comparison window. It is excluded here:
    including it makes the rank self-referential and compresses it toward the middle on
    small windows. The window is therefore the ``atr_pct_window`` values ending on the
    bar *before* the signal bar.

    Args:
        h1: Closed H1 bars, ascending.
        p: Strategy parameters.

    Returns:
        ``(regime, adx_value, atr_percentile_rank)``. The rank is NaN when the window is
        not yet full, in which case the regime is ``RANGING`` so that no caller can read
        an unwarmed indicator as tradeable.
    """
    if len(h1) < 2:
        return Regime.RANGING, float("nan"), float("nan")

    high = h1["high"].to_numpy(dtype=np.float64)
    low = h1["low"].to_numpy(dtype=np.float64)
    close = h1["close"].to_numpy(dtype=np.float64)

    adx_value = float(adx(high, low, close, p.adx_period)[-1])
    atr_series = atr(high, low, close, p.atr_period)
    with np.errstate(divide="ignore", invalid="ignore"):
        atr_pct_series = np.divide(atr_series, close,
                                   out=np.full_like(atr_series, np.nan), where=close > 0.0)

    atr_pct = float(atr_pct_series[-1])
    window = atr_pct_series[-(p.atr_pct_window + 1):-1]
    rank = (percentile_rank(atr_pct, window)
            if window.size == p.atr_pct_window else float("nan"))

    if not (math.isfinite(rank) and math.isfinite(adx_value)):
        return Regime.RANGING, adx_value, rank
    if rank > p.atr_pct_ceiling:
        return Regime.EXTREME, adx_value, rank
    if adx_value < p.adx_min or rank < p.atr_pct_floor:
        return Regime.RANGING, adx_value, rank
    return Regime.TRENDING, adx_value, rank
