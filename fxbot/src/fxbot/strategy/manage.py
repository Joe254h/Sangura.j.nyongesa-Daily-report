"""THE exit logic (§7.4). Pure: bars and one open position in, exactly one intent out.

Evaluated on every closed bar while a position is open, in this order:

1. hard invalidation on a strictly opposed daily bias below +0.5R,
2. partial take-profit at ``tp1_r``,
3. breakeven-plus-costs at ``breakeven_at_r``,
4. Chandelier trail,
5. nothing.

**One intent per bar, first match wins.** Rules 2 and 3 both fire around 1.0-1.5R; the
partial wins on that bar and breakeven applies on the next. Two intents are never batched
and a ``MODIFY_STOP`` never rides along with a ``CLOSE_PARTIAL`` -- MT5 needs separate
requests and a partial close can change the ticket.

Every R below is ``r_multiple(pos, ctx.h1.close[-1])``: the **close of the last closed
bar**, never a bar extreme and never a live tick. Using the favourable extreme would
assume an intrabar fill the backtester cannot reproduce and would break parity (§12.5).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from fxbot.config.schema import StrategyParams
from fxbot.core.enums import Bias, IntentKind, Side
from fxbot.core.models import Intent, Position, StrategyContext
from fxbot.indicators.atr import atr as atr_indicator
from fxbot.indicators.stats import r_multiple
from fxbot.strategy.base import partial_close_volume, round_stop_away
from fxbot.strategy.regime import htf_bias

_BIAS_FLIP_MAX_R = 0.5
"""Above this R a strictly opposed daily bias is left to the trailing stop, not cut."""


def _entry_bar_index(h1: pd.DataFrame, pos: Position) -> int:
    """Return the index of the bar the position was filled on.

    Bars are labelled by their OPEN time, and a fill happens at a bar's open, so the entry
    bar is the last bar whose open time is at or before ``pos.open_time``.
    """
    idx = int(h1.index.searchsorted(pos.open_time, side="right")) - 1
    return max(idx, 0)


def chandelier_stop(h1: pd.DataFrame, pos: Position, p: StrategyParams) -> float:
    """Return the Chandelier trailing stop for ``pos``.

    ``highest_high(since entry) - trail_atr_mult * ATR`` for a BUY, mirrored for a SELL.
    The window **includes the entry bar**, so the trail is defined from the first bar
    onward; combined with the ratchet rule in :func:`manage_position` the stop simply
    stays where it is until price makes progress, and no special case is needed.

    Args:
        h1: Closed H1 bars, ascending, indexed by bar open time.
        pos: The open position.
        p: Strategy parameters.

    Returns:
        The trailing stop price, or NaN when the ATR is not yet finite.
    """
    if len(h1) == 0:
        return float("nan")
    atr_value = float(atr_indicator(h1["high"].to_numpy(dtype=np.float64),
                                    h1["low"].to_numpy(dtype=np.float64),
                                    h1["close"].to_numpy(dtype=np.float64),
                                    p.atr_period)[-1])
    if not math.isfinite(atr_value):
        return float("nan")

    start = _entry_bar_index(h1, pos)
    if pos.side is Side.BUY:
        extreme = float(h1["high"].to_numpy(dtype=np.float64)[start:].max())
        return extreme - p.trail_atr_mult * atr_value
    extreme = float(h1["low"].to_numpy(dtype=np.float64)[start:].min())
    return extreme + p.trail_atr_mult * atr_value


def _stop_is_better(new_stop: float, current_stop: float, side: Side) -> bool:
    """Return whether ``new_stop`` protects more than ``current_stop``.

    A trailing stop must never widen (§17.13). A position whose broker stop is 0.0 -- one
    adopted during reconciliation before its stop was reconstructed -- accepts any stop.
    """
    if current_stop <= 0.0:
        return True
    return new_stop > current_stop if side is Side.BUY else new_stop < current_stop


def _placeable_stop(
    new_stop: float, ctx: StrategyContext, pos: Position, reason: str
) -> Intent:
    """Turn a desired stop into a ``MODIFY_STOP`` intent, or ``NONE`` if it cannot be placed.

    Applies, in order: rounding away from the entry, the broker's ``stops_level`` measured
    from the **current** price, the ``freeze_level`` zone, and the monotonic ratchet.
    """
    spec = ctx.spec
    price = float(ctx.h1["close"].to_numpy(dtype=np.float64)[-1])
    stop = round_stop_away(new_stop, pos.entry_price, spec.digits)

    min_distance = (spec.stops_level + max(ctx.current_spread_points, 0)) * spec.point
    if pos.side is Side.BUY:
        if stop > price - min_distance:
            return Intent(kind=IntentKind.NONE, symbol=ctx.symbol,
                          reason=f"{reason}:inside_stops_level")
    elif stop < price + min_distance:
        return Intent(kind=IntentKind.NONE, symbol=ctx.symbol,
                      reason=f"{reason}:inside_stops_level")

    if abs(price - stop) < spec.freeze_level * spec.point:
        # Inside the freeze zone the broker refuses modifications outright. Return NONE
        # and retry next bar rather than burning a rejected request every hour.
        return Intent(kind=IntentKind.NONE, symbol=ctx.symbol, reason=f"{reason}:freeze_zone")

    if not _stop_is_better(stop, pos.stop_loss, pos.side):
        return Intent(kind=IntentKind.NONE, symbol=ctx.symbol, reason=f"{reason}:not_better")

    return Intent(kind=IntentKind.MODIFY_STOP, symbol=ctx.symbol, side=pos.side,
                  stop_price=stop, take_profit=None, ticket=pos.ticket, reason=reason)


def breakeven_stop(ctx: StrategyContext, pos: Position) -> float:
    """Return the breakeven-plus-costs stop for ``pos``.

    A "breakeven" stop that ignores costs is a small guaranteed loss on every trade that
    reaches it. The buffer is the current spread plus the round-turn commission converted
    to price units at the symbol's own tick value.

    Args:
        ctx: The strategy context.
        pos: The open position.

    Returns:
        The stop price, on the profitable side of the entry by the cost buffer.
    """
    spec = ctx.spec
    spread_price = ctx.current_spread_points * spec.point
    value_per_unit = spec.value_per_price_unit_per_lot
    commission_price = (ctx.commission_per_lot_round_turn / value_per_unit
                        if value_per_unit > 0.0 else 0.0)
    buffer = spread_price + commission_price
    return pos.entry_price + buffer if pos.side is Side.BUY else pos.entry_price - buffer


def manage_position(ctx: StrategyContext) -> Intent:
    """Return the single action to take on the open position this bar.

    Args:
        ctx: The strategy context, whose ``open_position`` must not be None for any
            action to be produced.

    Returns:
        Exactly one :class:`~fxbot.core.models.Intent`.
    """
    pos = ctx.open_position
    if pos is None or len(ctx.h1) == 0:
        return Intent(kind=IntentKind.NONE, symbol=ctx.symbol, reason="no_position")

    # A sanity failure means a corrupt bar. Trailing off a corrupt high can ratchet a stop
    # into the market and close the trade at a garbage price, so suppress every action and
    # let the broker-side stop be the backstop (§6.4).
    if ctx.quality.fatal_sanity:
        return Intent(kind=IntentKind.NONE, symbol=ctx.symbol, reason="fatal_sanity")

    p = ctx.params
    close = float(ctx.h1["close"].to_numpy(dtype=np.float64)[-1])
    atr_value = float(atr_indicator(ctx.h1["high"].to_numpy(dtype=np.float64),
                                    ctx.h1["low"].to_numpy(dtype=np.float64),
                                    ctx.h1["close"].to_numpy(dtype=np.float64),
                                    p.atr_period)[-1])
    if not math.isfinite(atr_value):
        # NaN is never treated as zero (§7.4). No ATR, no management this bar.
        return Intent(kind=IntentKind.NONE, symbol=ctx.symbol, reason="atr_nan")

    r = r_multiple(pos, close)

    # Rule 1 - hard invalidation. NEUTRAL is NOT a flip: the neutral band exists precisely
    # to damp oscillation around the D1 EMA, and treating it as a flip would eject every
    # trade in a pair that pulls back to its daily mean (§7.4).
    bias = htf_bias(ctx.d1, p)
    opposed = ((bias is Bias.SHORT_ONLY and pos.side is Side.BUY)
               or (bias is Bias.LONG_ONLY and pos.side is Side.SELL))
    if opposed and r < _BIAS_FLIP_MAX_R:
        return Intent(kind=IntentKind.CLOSE, symbol=ctx.symbol, side=pos.side,
                      ticket=pos.ticket, reason="bias_flip")

    # Rule 2 - partial take-profit.
    if p.use_partials and not pos.partial_taken and r >= p.tp1_r:
        close_volume = partial_close_volume(pos.volume, p.tp1_fraction, ctx.spec)
        remaining = round(pos.volume - close_volume, 8)
        # The runner is where this strategy's expectancy lives: if banking the partial
        # would leave less than one minimum lot, skip the partial entirely rather than
        # closing the whole position.
        if close_volume >= ctx.spec.volume_min and remaining >= ctx.spec.volume_min:
            return Intent(kind=IntentKind.CLOSE_PARTIAL, symbol=ctx.symbol, side=pos.side,
                          close_fraction=p.tp1_fraction, ticket=pos.ticket, reason="tp1")

    # Rule 3 - breakeven plus costs.
    if r >= p.breakeven_at_r:
        be = breakeven_stop(ctx, pos)
        if _stop_is_better(be, pos.stop_loss, pos.side):
            return _placeable_stop(be, ctx, pos, "breakeven")

    # Rule 4 - Chandelier trail, monotonic ratchet.
    trail = chandelier_stop(ctx.h1, pos, p)
    if math.isfinite(trail) and _stop_is_better(trail, pos.stop_loss, pos.side):
        return _placeable_stop(trail, ctx, pos, "trail")

    return Intent(kind=IntentKind.NONE, symbol=ctx.symbol, reason="hold")
