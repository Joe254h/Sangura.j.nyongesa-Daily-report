"""Entry-logic tests: the six golden cases §7.5 requires, plus the gate ordering.

Each fixture is engineered so that exactly one gate refuses it. That is what makes the
reject-reason histogram (§10.3) a debugging tool rather than a rough indication.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import numpy as np
import pytest
from tests.conftest import SERVER_TZ, load_bars, make_context

from fxbot.core.enums import Bias, Regime, RejectReason, Side
from fxbot.indicators.donchian import donchian
from fxbot.strategy.trend_donchian import generate_signal


def signal_for(name: str, cfg, clock, eurusd, **kwargs):  # noqa: ANN001, ANN201
    """Build the context for a golden fixture and run the strategy over it."""
    return generate_signal(make_context(load_bars(name), eurusd, cfg, clock, **kwargs))


# ---------------------------------------------------------------- the six golden cases


def test_golden_clean_long_breakout(cfg, clock, eurusd) -> None:
    """A daily uptrend, a trending H1, a close through the channel, EMA agreeing."""
    signal = signal_for("clean_long", cfg, clock, eurusd)
    assert signal.side is Side.BUY
    assert signal.reason is RejectReason.NONE
    assert signal.regime is Regime.TRENDING
    assert signal.bias is Bias.LONG_ONLY
    assert signal.stop_price < signal.entry_ref
    assert signal.stop_price == pytest.approx(
        signal.entry_ref - cfg.strategy.sl_atr_mult * signal.atr, abs=1e-5)


def test_golden_clean_short_breakout(cfg, clock, eurusd) -> None:
    """The mirror image, with the stop above the entry."""
    signal = signal_for("clean_short", cfg, clock, eurusd)
    assert signal.side is Side.SELL
    assert signal.reason is RejectReason.NONE
    assert signal.bias is Bias.SHORT_ONLY
    assert signal.stop_price > signal.entry_ref


def test_golden_adx_blocked(cfg, clock, eurusd) -> None:
    """A breakout the ADX floor refuses. The reason is REGIME, not NO_TRIGGER."""
    signal = signal_for("adx_blocked", cfg, clock, eurusd)
    assert signal.side is None
    assert signal.reason is RejectReason.REGIME
    assert signal.regime is Regime.RANGING
    assert signal.diagnostics["adx"] < cfg.strategy.adx_min


def test_golden_volatility_ceiling_blocked(cfg, clock, eurusd) -> None:
    """A news blowout is EXTREME and is refused before the trigger is even looked at."""
    signal = signal_for("vol_ceiling", cfg, clock, eurusd)
    assert signal.side is None
    assert signal.reason is RejectReason.REGIME
    assert signal.regime is Regime.EXTREME
    assert signal.diagnostics["atr_pct_rank"] > cfg.strategy.atr_pct_ceiling


def test_golden_bias_neutral_blocked(cfg, clock, eurusd) -> None:
    """A neutral daily bias short-circuits at step 1, before any H1 work."""
    signal = signal_for("bias_neutral", cfg, clock, eurusd)
    assert signal.side is None
    assert signal.reason is RejectReason.BIAS
    assert signal.bias is Bias.NEUTRAL
    assert "adx" not in signal.diagnostics, "step 1 must short-circuit before step 2"


def test_golden_donchian_lookahead_regression(cfg, clock, eurusd) -> None:
    """The signal bar's own high must not define the channel it has to break (§17.3).

    The fixture's last bar wicks well above the channel but closes below it. An
    implementation that triggers on the bar's extreme -- or that builds the channel to
    exclude the signal bar and then reads ``[-1]`` -- fires here. The ``[-2]`` rule does
    not.
    """
    frame = load_bars("donchian_lookahead")
    signal = generate_signal(make_context(frame, eurusd, cfg, clock))
    assert signal.side is None
    assert signal.reason is RejectReason.NO_TRIGGER

    high = frame["high"].to_numpy(float)
    low = frame["low"].to_numpy(float)
    period = cfg.strategy.donchian_period
    upper, lower = donchian(high, low, period)
    assert signal.diagnostics["donchian_upper"] == pytest.approx(float(upper[-2]))
    assert signal.diagnostics["donchian_upper"] == pytest.approx(
        float(np.max(high[-(period + 1):-1])))
    assert signal.diagnostics["donchian_lower"] == pytest.approx(float(lower[-2]))
    assert high[-1] > signal.diagnostics["donchian_upper"], "the wick did clear the channel"
    assert frame["close"].to_numpy(float)[-1] < signal.diagnostics["donchian_upper"]


# ---------------------------------------------------------------- gate ordering


def test_session_gate_refuses_out_of_hours(cfg, clock, eurusd) -> None:
    """A perfect setup at 03:00 server is still refused, with SESSION_CLOSED."""
    ctx = make_context(load_bars("clean_long"), eurusd, cfg, clock)
    out_of_hours = replace(ctx, now=datetime(2024, 3, 27, 3, 0, tzinfo=SERVER_TZ))
    signal = generate_signal(out_of_hours)
    assert signal.reason is RejectReason.SESSION_CLOSED
    assert signal.side is None


def test_spread_gate_refuses_a_blown_out_spread(cfg, clock, eurusd) -> None:
    """The spread is checked at signal time and again before the order is sent (§9.2)."""
    signal = signal_for("clean_long", cfg, clock, eurusd, spread_points=200)
    assert signal.reason is RejectReason.SPREAD_TOO_WIDE
    assert signal.diagnostics["spread"] == 200


def test_confirmation_is_a_distinct_reason_from_no_trigger(cfg, clock, eurusd) -> None:
    """The histogram must separate "no breakout" from "a breakout the trend disliked"."""
    faster = cfg.strategy.model_copy(update={"ema_fast": 200, "ema_slow": 250})
    ctx = replace(make_context(load_bars("clean_long"), eurusd, cfg, clock), params=faster)
    signal = generate_signal(ctx)
    assert signal.reason in (RejectReason.CONFIRMATION, RejectReason.NONE)
    if signal.reason is RejectReason.CONFIRMATION:
        assert signal.diagnostics["ema_fast"] <= signal.diagnostics["ema_slow"]


def test_a_quality_failure_blocks_the_entry(cfg, clock, eurusd) -> None:
    """Fail closed: an unusable frame never produces a side (§0.7)."""
    from fxbot.core.models import QualityReport

    bad = QualityReport(False, RejectReason.STALE_DATA, "gap", 10,
                        datetime(2024, 3, 27, tzinfo=SERVER_TZ), 9, False)
    signal = signal_for("clean_long", cfg, clock, eurusd, quality=bad)
    assert signal.side is None
    assert signal.reason is RejectReason.STALE_DATA


def test_too_few_bars_rejects_before_any_indicator(cfg, clock, eurusd) -> None:
    """Below ``warmup_bars`` the strategy refuses rather than reading a NaN as zero."""
    frame = load_bars("clean_long").iloc[-50:]
    signal = generate_signal(make_context(frame, eurusd, cfg, clock))
    assert signal.side is None
    assert signal.reason is RejectReason.STALE_DATA


def test_the_stop_respects_the_broker_minimum_distance(cfg, clock, eurusd) -> None:
    """A symbol with a wide ``stops_level`` gets its stop widened, never narrowed."""
    wide = replace(eurusd, stops_level=400)
    ctx = make_context(load_bars("clean_long"), eurusd, cfg, clock)
    signal = generate_signal(replace(ctx, spec=wide))
    assert signal.side is Side.BUY
    minimum = wide.min_stop_distance(ctx.current_spread_points)
    assert signal.entry_ref - signal.stop_price >= minimum - 1e-9


def test_stop_rounding_never_moves_toward_the_entry(cfg, clock, eurusd) -> None:
    """Rounding toward the entry silently increases risk on every trade (§7.3 step 8)."""
    signal = signal_for("clean_long", cfg, clock, eurusd)
    raw = signal.diagnostics["raw_stop"]
    assert signal.stop_price <= raw + 1e-12
    assert round(signal.stop_price, eurusd.digits) == pytest.approx(signal.stop_price)


def test_the_signal_is_a_pure_function_of_the_context(cfg, clock, eurusd) -> None:
    """Same context in, same signal out, always -- and ``ctx`` is not mutated (§7.5)."""
    ctx = make_context(load_bars("clean_long"), eurusd, cfg, clock)
    before = ctx.h1.copy(deep=True)
    first, second = generate_signal(ctx), generate_signal(ctx)
    assert first == second
    pd_testing_equal(before, ctx.h1)


def pd_testing_equal(left, right) -> None:  # noqa: ANN001
    """Assert two frames are identical, including dtypes."""
    import pandas as pd

    pd.testing.assert_frame_equal(left, right)


def test_diagnostics_carry_every_value_the_decision_used(cfg, clock, eurusd) -> None:
    """A losing month has to be diagnosable, not mysterious (§7.3 step 8)."""
    signal = signal_for("clean_long", cfg, clock, eurusd)
    for key in ("d1_ema", "d1_atr", "adx", "atr", "atr_pct_rank", "donchian_upper",
                "donchian_lower", "ema_fast", "ema_slow", "spread"):
        assert key in signal.diagnostics, key
        assert np.isfinite(signal.diagnostics[key]), key
