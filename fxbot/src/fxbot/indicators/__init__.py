"""Pure indicator functions.

Every function returns an array the **same length as its input**, NaN-padded at the front.
Nothing here ever calls ``dropna()``: silently shortening an array makes the index drift
between the backtest and the live engine and breaks parity (§7.5).
"""

from fxbot.indicators.adx import adx
from fxbot.indicators.atr import atr, true_range
from fxbot.indicators.donchian import donchian
from fxbot.indicators.ema import ema
from fxbot.indicators.stats import percentile_rank, r_multiple

__all__ = ["adx", "atr", "donchian", "ema", "percentile_rank", "r_multiple", "true_range"]
