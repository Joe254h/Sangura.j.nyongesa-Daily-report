"""THE strategy: H1 Donchian breakouts filtered by daily trend and H1 regime (§7.3).

The eight gates are evaluated in the exact order §7.3 gives, short-circuiting on the
first failure, and every refusal carries its own :class:`~fxbot.core.enums.RejectReason`.
That ordering is not cosmetic: it is what makes the reject-reason histogram (§10.3) the
primary debugging tool. If the common case -- no breakout at all -- were allowed to fall
through to the confirmation gate, every quiet bar would be attributed to a failed EMA
confirmation and the histogram would be useless.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from fxbot.core.enums import Bias, Regime, RejectReason, Side
from fxbot.core.models import Signal, StrategyContext
from fxbot.indicators.atr import atr as atr_indicator
from fxbot.indicators.donchian import donchian
from fxbot.indicators.ema import ema
from fxbot.strategy.base import enforce_stop_distance
from fxbot.strategy.regime import classify_regime, htf_bias

_FRIDAY = 4
_SATURDAY = 5
_SUNDAY = 6
_MONDAY = 0


def in_session(ctx: StrategyContext) -> bool:
    """Return whether ``ctx.now`` passes the session gate (§7.3 step 3).

    Evaluated here rather than on :class:`~fxbot.core.clock.ServerClock` because no clock
    object may cross the purity boundary (§0.2): the strategy sees only the hour of
    ``ctx.now``, which is already broker server time, and the frozen
    :class:`~fxbot.config.schema.SessionParams` on the context. The week-open convention
    matches :meth:`fxbot.core.clock.ServerClock.in_session` exactly -- the two are tested
    against each other in ``tests/test_clock.py``.

    Args:
        ctx: The strategy context.

    Returns:
        True when new entries are permitted at this bar's close.
    """
    s = ctx.session
    now = ctx.now
    weekday = now.weekday()

    if weekday == _SATURDAY:
        return False
    if now.hour not in s.trade_hours_server:
        return False
    if weekday == _FRIDAY and now.hour > s.skip_friday_after_hour:
        return False
    if s.skip_hours_after_weekend_open > 0:
        if weekday == _SUNDAY:
            return False
        if weekday == _MONDAY and now.hour < s.skip_hours_after_weekend_open:
            return False
    return True


def _reject(
    reason: RejectReason,
    *,
    regime: Regime = Regime.RANGING,
    bias: Bias = Bias.NEUTRAL,
    entry_ref: float = 0.0,
    atr_value: float = float("nan"),
    adx_value: float = float("nan"),
    diagnostics: dict[str, float] | None = None,
) -> Signal:
    """Build a rejecting :class:`Signal` carrying everything computed so far."""
    return Signal(
        side=None,
        regime=regime,
        bias=bias,
        entry_ref=entry_ref,
        stop_price=0.0,
        atr=atr_value,
        adx=adx_value,
        reason=reason,
        diagnostics=dict(diagnostics or {}),
    )


def generate_signal(ctx: StrategyContext) -> Signal:
    """Return the entry decision for the just-closed bar.

    A pure function of ``ctx``: same context in, same signal out, always. No ``random``,
    no clock read, no I/O, no mutation of ``ctx`` (§7.5).

    Args:
        ctx: Everything the decision may look at.

    Returns:
        A :class:`~fxbot.core.models.Signal`. ``side`` is None for every rejection and
        ``reason`` names which gate refused.
    """
    p = ctx.params
    diag: dict[str, float] = {"spread": float(ctx.current_spread_points)}

    # Step 0 - data quality. Not one of §7.3's eight gates, but §0.7 says any uncertainty
    # halts new entries, and §10.2 step 7 hands management a context whose quality report
    # failed rather than raising. Checking it here means the pure layer fails closed even
    # if a future caller forgets to.
    if not ctx.quality.ok:
        return _reject(ctx.quality.reason, diagnostics=diag)
    if len(ctx.h1) < p.warmup_bars:
        return _reject(RejectReason.STALE_DATA, diagnostics=diag)

    close = ctx.h1["close"].to_numpy(dtype=np.float64)
    entry_ref = float(close[-1])
    diag["close"] = entry_ref

    # Step 1 - higher-timeframe bias.
    bias = htf_bias(ctx.d1, p)
    diag.update(_d1_diagnostics(ctx.d1, p))
    if bias is Bias.NEUTRAL:
        return _reject(RejectReason.BIAS, bias=bias, entry_ref=entry_ref, diagnostics=diag)

    # Step 2 - regime.
    regime, adx_value, atr_rank = classify_regime(ctx.h1, p)
    atr_value = _last_atr(ctx.h1, p)
    diag.update({"adx": adx_value, "atr": atr_value, "atr_pct_rank": atr_rank})
    if regime is not Regime.TRENDING:
        return _reject(RejectReason.REGIME, regime=regime, bias=bias, entry_ref=entry_ref,
                       atr_value=atr_value, adx_value=adx_value, diagnostics=diag)
    if not math.isfinite(atr_value) or atr_value <= 0.0:
        return _reject(RejectReason.REGIME, regime=regime, bias=bias, entry_ref=entry_ref,
                       atr_value=atr_value, adx_value=adx_value, diagnostics=diag)

    common: dict[str, Any] = {
        "regime": regime, "bias": bias, "entry_ref": entry_ref,
        "atr_value": atr_value, "adx_value": adx_value,
    }

    # Step 3 - session.
    if not in_session(ctx):
        return _reject(RejectReason.SESSION_CLOSED, diagnostics=diag, **common)

    # Step 4 - spread gate. Checked again immediately before the order is sent (§9.2).
    if ctx.current_spread_points > ctx.max_spread_points:
        return _reject(RejectReason.SPREAD_TOO_WIDE, diagnostics=diag, **common)

    # Step 5 - Donchian trigger on the closed bar. The channel EXCLUDES the signal bar:
    # using upper[-1] makes the breakout self-referential (the bar's own high defines the
    # channel it must break) and produces an inflated, untradeable backtest (§17.3).
    upper, lower = donchian(ctx.h1["high"].to_numpy(dtype=np.float64),
                            ctx.h1["low"].to_numpy(dtype=np.float64),
                            p.donchian_period)
    channel_upper = float(upper[-2])
    channel_lower = float(lower[-2])
    diag.update({"donchian_upper": channel_upper, "donchian_lower": channel_lower})
    if not (math.isfinite(channel_upper) and math.isfinite(channel_lower)):
        return _reject(RejectReason.REGIME, diagnostics=diag, **common)

    long_trigger = entry_ref > channel_upper
    short_trigger = entry_ref < channel_lower
    # Mutually exclusive: upper[-2] >= lower[-2] always holds, so a close cannot be both
    # above the upper band and below the lower band. No tie-break is needed.
    if not long_trigger and not short_trigger:
        return _reject(RejectReason.NO_TRIGGER, diagnostics=diag, **common)
    side = Side.BUY if long_trigger else Side.SELL

    # Step 6 - EMA confirmation. A distinct reason from NO_TRIGGER, so the histogram can
    # separate "no breakout" from "a breakout the trend did not agree with".
    ema_fast = float(ema(close, p.ema_fast)[-1])
    ema_slow = float(ema(close, p.ema_slow)[-1])
    diag.update({"ema_fast": ema_fast, "ema_slow": ema_slow})
    if not (math.isfinite(ema_fast) and math.isfinite(ema_slow)):
        return _reject(RejectReason.REGIME, diagnostics=diag, **common)
    confirmed = ema_fast > ema_slow if side is Side.BUY else ema_fast < ema_slow
    if not confirmed:
        return _reject(RejectReason.CONFIRMATION, diagnostics=diag, **common)

    # Step 7 - direction must match the daily bias.
    if (side is Side.BUY and bias is not Bias.LONG_ONLY) or (
        side is Side.SELL and bias is not Bias.SHORT_ONLY
    ):
        return _reject(RejectReason.BIAS, diagnostics=diag, **common)

    # Step 8 - stop placement, then the broker minimum.
    raw_stop = (entry_ref - p.sl_atr_mult * atr_value if side is Side.BUY
                else entry_ref + p.sl_atr_mult * atr_value)
    stop = enforce_stop_distance(raw_stop, entry_ref, side, ctx.spec, ctx.current_spread_points)
    diag["raw_stop"] = raw_stop
    diag["stop"] = stop

    return Signal(
        side=side,
        regime=regime,
        bias=bias,
        entry_ref=entry_ref,
        stop_price=stop,
        atr=atr_value,
        adx=adx_value,
        reason=RejectReason.NONE,
        diagnostics=diag,
    )


def _last_atr(h1: pd.DataFrame, p: Any) -> float:
    """Return the ATR of the last closed H1 bar, or NaN."""
    if len(h1) < p.atr_period + 1:
        return float("nan")
    return float(atr_indicator(h1["high"].to_numpy(dtype=np.float64),
                               h1["low"].to_numpy(dtype=np.float64),
                               h1["close"].to_numpy(dtype=np.float64),
                               p.atr_period)[-1])


def _d1_diagnostics(d1: pd.DataFrame, p: Any) -> dict[str, float]:
    """Return the daily EMA and ATR used by the bias gate, for the journal."""
    if len(d1) < max(p.d1_ema, p.atr_period) + 1:
        return {"d1_ema": float("nan"), "d1_atr": float("nan"), "d1_close": float("nan")}
    close = d1["close"].to_numpy(dtype=np.float64)
    return {
        "d1_ema": float(ema(close, p.d1_ema)[-1]),
        "d1_atr": float(atr_indicator(d1["high"].to_numpy(dtype=np.float64),
                                      d1["low"].to_numpy(dtype=np.float64),
                                      close, p.atr_period)[-1]),
        "d1_close": float(close[-1]),
    }
