"""Position-management tests (§7.4).

The interactions matter more than the rules: rules 2 and 3 both fire around 1.0-1.5R, and
this is where two implementations silently diverge.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from tests.conftest import load_bars, make_context, make_position

from fxbot.core.enums import IntentKind, Side
from fxbot.strategy.manage import breakeven_stop, chandelier_stop, manage_position

LONG_FIXTURE = "clean_long"
SHORT_FIXTURE = "clean_short"


def setup(name, cfg, clock, spec, side, *, r_target, bars_ago=40, volume=0.10,
          stop_loss=None, partial_taken=False):  # noqa: ANN001, ANN201
    """Build a context with a position whose R at the last close is ``r_target``."""
    frame = load_bars(name)
    close = float(frame["close"].to_numpy()[-1])
    entry_time = frame.index[-bars_ago].to_pydatetime()
    entry = float(frame["close"].to_numpy()[-bars_ago])
    # Choose the initial stop so that r_multiple(pos, close) == r_target exactly.
    move = (close - entry) * side.sign
    risk = move / r_target if r_target else 0.01
    stop = entry - risk * side.sign
    position = make_position(spec, side, entry, stop, entry_time, volume=volume,
                             stop_loss=stop_loss, partial_taken=partial_taken)
    return make_context(frame, spec, cfg, clock, position=position), position


def test_no_position_is_a_no_op(cfg, clock, eurusd) -> None:
    """Management of nothing does nothing."""
    ctx = make_context(load_bars(LONG_FIXTURE), eurusd, cfg, clock)
    assert manage_position(ctx).kind is IntentKind.NONE


def test_partial_take_profit_fires_at_tp1(cfg, clock, eurusd) -> None:
    """At 1.5R the runner banks half, once."""
    ctx, _ = setup(LONG_FIXTURE, cfg, clock, eurusd, Side.BUY, r_target=1.8)
    intent = manage_position(ctx)
    assert intent.kind is IntentKind.CLOSE_PARTIAL
    assert intent.close_fraction == pytest.approx(cfg.strategy.tp1_fraction)
    assert intent.reason == "tp1"


def test_the_partial_wins_the_bar_and_breakeven_waits(cfg, clock, eurusd) -> None:
    """One intent per bar, first match wins: rules 2 and 3 overlap and 2 comes first."""
    ctx, position = setup(LONG_FIXTURE, cfg, clock, eurusd, Side.BUY, r_target=1.6)
    assert manage_position(ctx).kind is IntentKind.CLOSE_PARTIAL
    # Next bar, with the partial already taken, breakeven applies.
    after = replace(ctx, open_position=replace(position, partial_taken=True))
    assert manage_position(after).kind is IntentKind.MODIFY_STOP


def test_partial_is_skipped_when_the_runner_would_fall_below_minimum(cfg, clock,
                                                                     eurusd) -> None:
    """The runner is where the expectancy lives: skip the partial, never close it all."""
    ctx, _ = setup(LONG_FIXTURE, cfg, clock, eurusd, Side.BUY, r_target=1.8, volume=0.01)
    intent = manage_position(ctx)
    assert intent.kind is not IntentKind.CLOSE_PARTIAL
    assert intent.kind is not IntentKind.CLOSE


def test_breakeven_stop_covers_costs(cfg, clock, eurusd) -> None:
    """A breakeven stop that ignores costs is a small guaranteed loss (§7.4 rule 3)."""
    ctx, position = setup(LONG_FIXTURE, cfg, clock, eurusd, Side.BUY, r_target=1.2,
                          partial_taken=True)
    stop = breakeven_stop(ctx, position)
    assert stop > position.entry_price
    spread_price = ctx.current_spread_points * eurusd.point
    commission_price = ctx.commission_per_lot_round_turn / eurusd.value_per_price_unit_per_lot
    assert stop == pytest.approx(position.entry_price + spread_price + commission_price)


def test_breakeven_does_not_fire_below_the_threshold(cfg, clock, eurusd) -> None:
    """At 0.4R there is nothing to protect yet."""
    ctx, _ = setup(LONG_FIXTURE, cfg, clock, eurusd, Side.BUY, r_target=0.4)
    intent = manage_position(ctx)
    assert intent.kind in (IntentKind.NONE, IntentKind.MODIFY_STOP)
    if intent.kind is IntentKind.MODIFY_STOP:
        assert intent.reason.startswith("trail")


def test_chandelier_includes_the_entry_bar(cfg, clock, eurusd) -> None:
    """``highest_high(since entry)`` includes the entry bar, so the trail is always defined."""
    frame = load_bars(LONG_FIXTURE)
    entry_index = -30
    position = make_position(eurusd, Side.BUY, float(frame["close"].to_numpy()[entry_index]),
                             float(frame["close"].to_numpy()[entry_index]) - 0.0020,
                             frame.index[entry_index].to_pydatetime())
    stop = chandelier_stop(frame, position, cfg.strategy)
    highs = frame["high"].to_numpy(float)[entry_index:]
    from fxbot.indicators.atr import atr

    atr_value = float(atr(frame["high"].to_numpy(float), frame["low"].to_numpy(float),
                          frame["close"].to_numpy(float), cfg.strategy.atr_period)[-1])
    assert stop == pytest.approx(float(highs.max()) - cfg.strategy.trail_atr_mult * atr_value)


def test_the_trailing_stop_never_widens(cfg, clock, eurusd) -> None:
    """A trailing stop that can move against the position is a hard ban (§17.13)."""
    ctx, position = setup(LONG_FIXTURE, cfg, clock, eurusd, Side.BUY, r_target=3.0,
                          partial_taken=True)
    generous = replace(position, stop_loss=position.entry_price + 0.0050)
    intent = manage_position(replace(ctx, open_position=generous))
    if intent.kind is IntentKind.MODIFY_STOP:
        assert intent.stop_price is not None
        assert intent.stop_price > generous.stop_loss
    else:
        assert intent.kind is IntentKind.NONE


def test_the_freeze_zone_defers_rather_than_sending_a_doomed_request(cfg, clock,
                                                                     eurusd) -> None:
    """Inside the freeze level the broker refuses outright; retry next bar (§7.4 rule 4)."""
    ctx, position = setup(LONG_FIXTURE, cfg, clock, eurusd, Side.BUY, r_target=3.0,
                          partial_taken=True)
    frozen = replace(ctx, spec=replace(eurusd, freeze_level=100_000))
    intent = manage_position(frozen)
    assert intent.kind is IntentKind.NONE


def test_a_strictly_opposed_daily_bias_closes_a_losing_trade(cfg, clock, eurusd) -> None:
    """Rule 1: opposed bias below +0.5R does not wait for the stop."""
    frame = load_bars(SHORT_FIXTURE)   # the daily bias here is SHORT_ONLY
    close = float(frame["close"].to_numpy()[-1])
    position = make_position(eurusd, Side.BUY, close + 0.0010, close - 0.0040,
                             frame.index[-30].to_pydatetime())
    ctx = make_context(frame, eurusd, cfg, clock, position=position)
    intent = manage_position(ctx)
    assert intent.kind is IntentKind.CLOSE
    assert intent.reason == "bias_flip"


def test_a_neutral_bias_is_not_a_flip(cfg, clock, eurusd) -> None:
    """NEUTRAL exists to damp oscillation; treating it as a flip ejects every pullback."""
    frame = load_bars("bias_neutral")
    close = float(frame["close"].to_numpy()[-1])
    position = make_position(eurusd, Side.BUY, close + 0.0010, close - 0.0040,
                             frame.index[-30].to_pydatetime())
    ctx = make_context(frame, eurusd, cfg, clock, position=position)
    assert manage_position(ctx).kind is not IntentKind.CLOSE


def test_an_opposed_bias_above_half_r_is_left_to_the_trail(cfg, clock, eurusd) -> None:
    """A trade already in profit is managed by its stop, not cut on a daily flip."""
    frame = load_bars(SHORT_FIXTURE)
    close = float(frame["close"].to_numpy()[-1])
    position = make_position(eurusd, Side.SELL, close + 0.0100, close + 0.0140,
                             frame.index[-40].to_pydatetime())
    ctx = make_context(frame, eurusd, cfg, clock, position=position)
    assert manage_position(ctx).kind is not IntentKind.CLOSE


def test_a_fatal_sanity_failure_suppresses_every_action(cfg, clock, eurusd) -> None:
    """Trailing off a corrupt high can ratchet a stop into the market (§6.4)."""
    from fxbot.core.enums import RejectReason
    from fxbot.core.models import QualityReport

    ctx, _ = setup(LONG_FIXTURE, cfg, clock, eurusd, Side.BUY, r_target=3.0,
                   partial_taken=True)
    corrupt = replace(ctx, quality=QualityReport(
        False, RejectReason.STALE_DATA, "bad tick", ctx.quality.bars, ctx.quality.last_close,
        0, True))
    intent = manage_position(corrupt)
    assert intent.kind is IntentKind.NONE
    assert intent.reason == "fatal_sanity"


def test_a_nan_atr_is_never_treated_as_zero(cfg, clock, eurusd) -> None:
    """No ATR, no management this bar (§7.4)."""
    frame = load_bars(LONG_FIXTURE).iloc[-5:]
    position = make_position(eurusd, Side.BUY, 1.10, 1.09, frame.index[0].to_pydatetime())
    ctx = make_context(frame, eurusd, cfg, clock, position=position)
    intent = manage_position(ctx)
    assert intent.kind is IntentKind.NONE
    assert intent.reason == "atr_nan"
    assert np.isnan(chandelier_stop(frame.iloc[:2], position, cfg.strategy))


def test_r_is_measured_at_the_close_not_the_bar_extreme(cfg, clock, eurusd) -> None:
    """Using the favourable extreme assumes an intrabar fill and breaks parity (§7.4)."""
    frame = load_bars(LONG_FIXTURE)
    close = float(frame["close"].to_numpy()[-1])
    high = float(frame["high"].to_numpy()[-1])
    assert high > close
    entry = close - 0.0010
    # Risk sized so the HIGH is above tp1_r but the CLOSE is not.
    risk = (high - entry) / (cfg.strategy.tp1_r * 0.98)
    position = make_position(eurusd, Side.BUY, entry, entry - risk,
                             frame.index[-30].to_pydatetime())
    ctx = make_context(frame, eurusd, cfg, clock, position=position)
    assert (close - entry) / risk < cfg.strategy.tp1_r
    assert manage_position(ctx).kind is not IntentKind.CLOSE_PARTIAL
