"""Indicator tests (§12.3).

Each indicator is validated against a hand-computed fixture. **Wilder's smoothing is not
an SMA**, so the tests assert the exact recurrence rather than "close enough to a moving
average".
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest
from tests.conftest import SERVER_TZ, load_bars, make_position

from fxbot.core.enums import Side
from fxbot.core.models import SymbolSpec
from fxbot.indicators.adx import adx, directional_movement
from fxbot.indicators.atr import atr, true_range, wilder_smooth
from fxbot.indicators.donchian import donchian
from fxbot.indicators.ema import ema
from fxbot.indicators.stats import percentile_rank, r_multiple

HAND = np.array([
    10.0, 10.5, 11.0, 10.8, 11.2, 11.6, 11.4, 12.0, 12.4, 12.1,
    12.6, 13.0, 12.7, 13.2, 13.6, 13.3, 13.9, 14.2, 14.0, 14.5,
    14.9, 14.6, 15.1, 15.5, 15.2, 15.8, 16.1, 15.9, 16.4, 16.8,
])
"""A 30-row hand-checkable series."""


def test_lengths_are_preserved_and_nan_padded() -> None:
    """Every indicator returns the input length, NaN-padded at the front (§7.5)."""
    high = HAND + 0.3
    low = HAND - 0.3
    for values in (ema(HAND, 10), atr(high, low, HAND, 14), adx(high, low, HAND, 5)):
        assert len(values) == len(HAND)
    upper, lower = donchian(high, low, 5)
    assert len(upper) == len(lower) == len(HAND)


def test_ema_seeds_on_the_simple_mean_then_follows_the_recurrence() -> None:
    """The seed is the SMA of the first ``period`` values; then alpha = 2/(n+1)."""
    period = 5
    out = ema(HAND, period)
    assert np.all(np.isnan(out[:period - 1]))
    assert out[period - 1] == pytest.approx(HAND[:period].mean())
    alpha = 2.0 / (period + 1.0)
    expected = out[period - 1]
    for i in range(period, len(HAND)):
        expected = alpha * HAND[i] + (1 - alpha) * expected
        assert out[i] == pytest.approx(expected, abs=1e-12)


def test_true_range_uses_the_previous_close() -> None:
    """TR[0] has no previous close; every later bar takes the three-way maximum."""
    high = np.array([10.0, 11.0, 10.5])
    low = np.array([9.0, 10.2, 9.8])
    close = np.array([9.5, 10.9, 10.0])
    tr = true_range(high, low, close)
    assert tr[0] == pytest.approx(1.0)
    assert tr[1] == pytest.approx(max(11.0 - 10.2, abs(11.0 - 9.5), abs(10.2 - 9.5)))
    assert tr[2] == pytest.approx(max(10.5 - 9.8, abs(10.5 - 10.9), abs(9.8 - 10.9)))


def test_wilder_smoothing_is_not_an_sma() -> None:
    """``s[i] = (s[i-1]*(n-1) + v[i]) / n`` -- alpha is 1/n, not 2/(n+1)."""
    values = np.arange(1.0, 21.0)
    period = 4
    out = wilder_smooth(values, period)
    assert out[period - 1] == pytest.approx(values[:period].mean())
    expected = out[period - 1]
    for i in range(period, len(values)):
        expected = (expected * (period - 1) + values[i]) / period
        assert out[i] == pytest.approx(expected, abs=1e-12)
    # An SMA of the same window would be materially different by the end.
    assert out[-1] != pytest.approx(values[-period:].mean())


def test_wilder_smoothing_steps_over_a_leading_nan_pad() -> None:
    """ADX smooths DX, which is itself NaN for its first bars; bailing would kill ADX."""
    values = np.concatenate([np.full(5, np.nan), np.arange(1.0, 15.0)])
    out = wilder_smooth(values, 4)
    assert np.all(np.isnan(out[:8]))
    assert np.isfinite(out[8])


def test_atr_first_finite_value_lands_at_period_minus_one() -> None:
    """Wilder's ATR is seeded on the mean of the first ``period`` true ranges."""
    high, low = HAND + 0.4, HAND - 0.4
    out = atr(high, low, HAND, 14)
    assert np.all(np.isnan(out[:13]))
    assert out[13] == pytest.approx(true_range(high, low, HAND)[:14].mean())


def test_adx_first_finite_value_lands_at_twice_the_period() -> None:
    """One period smooths the directional movement, another smooths DX (§12.3)."""
    frame = load_bars("clean_long")
    out = adx(frame["high"].to_numpy(), frame["low"].to_numpy(),
              frame["close"].to_numpy(), 14)
    first = int(np.argmax(np.isfinite(out)))
    assert first == 2 * 14 - 1
    assert 0.0 <= out[-1] <= 100.0


def test_directional_movement_credits_only_the_larger_side() -> None:
    """An inside bar produces neither +DM nor -DM; an outside bar produces exactly one."""
    high = np.array([10.0, 11.0, 10.5, 12.0])
    low = np.array([9.0, 9.5, 9.8, 8.0])
    plus, minus = directional_movement(high, low)
    assert plus[0] == minus[0] == 0.0
    assert plus[1] == pytest.approx(1.0) and minus[1] == 0.0
    assert plus[2] == 0.0 and minus[2] == 0.0
    assert minus[3] == pytest.approx(1.8) and plus[3] == 0.0


def test_donchian_includes_the_current_bar() -> None:
    """The channel here includes bar i; the strategy is what excludes it, via ``[-2]``."""
    high = np.array([1.0, 3.0, 2.0, 5.0, 4.0])
    low = np.array([0.5, 1.5, 1.0, 2.0, 1.8])
    upper, lower = donchian(high, low, 3)
    assert np.isnan(upper[0]) and np.isnan(upper[1])
    assert upper[2] == pytest.approx(3.0)
    assert upper[3] == pytest.approx(5.0)
    assert lower[4] == pytest.approx(1.0)


def test_percentile_rank_counts_strictly_below() -> None:
    """The rank is the fraction of the window strictly less than x."""
    window = np.array([1.0, 2.0, 3.0, 4.0])
    assert percentile_rank(2.5, window) == pytest.approx(0.5)
    assert percentile_rank(0.0, window) == pytest.approx(0.0)
    assert percentile_rank(9.0, window) == pytest.approx(1.0)


def test_percentile_rank_returns_nan_on_an_unfilled_window() -> None:
    """Never 0.0 on an unfilled window -- that reads as 'lowest volatility ever' (§7.2)."""
    assert np.isnan(percentile_rank(1.0, np.array([1.0, np.nan, 3.0])))
    assert np.isnan(percentile_rank(np.nan, np.array([1.0, 2.0])))
    assert np.isnan(percentile_rank(1.0, np.array([])))


def test_r_multiple_measures_against_the_initial_stop(eurusd: SymbolSpec) -> None:
    """R uses initial_stop and the original entry, never the current trailing stop."""
    when = datetime(2024, 3, 1, 10, tzinfo=SERVER_TZ)
    long = make_position(eurusd, Side.BUY, 1.1000, 1.0980, when, stop_loss=1.1050)
    assert r_multiple(long, 1.1020) == pytest.approx(1.0)
    assert r_multiple(long, 1.0980) == pytest.approx(-1.0)
    short = make_position(eurusd, Side.SELL, 1.1000, 1.1020, when)
    assert r_multiple(short, 1.0960) == pytest.approx(2.0)


def test_r_multiple_is_zero_when_there_was_no_risk(eurusd: SymbolSpec) -> None:
    """A zero denominator returns 0.0 rather than raising or returning inf."""
    when = datetime(2024, 3, 1, 10, tzinfo=SERVER_TZ)
    flat = make_position(eurusd, Side.BUY, 1.1000, 1.1000, when)
    assert r_multiple(flat, 1.2000) == 0.0


def test_indicators_reject_malformed_input() -> None:
    """Failure paths: mismatched lengths and non-positive periods raise, never guess."""
    with pytest.raises(ValueError):
        ema(np.zeros((2, 2)), 5)
    with pytest.raises(ValueError):
        ema(HAND, 0)
    with pytest.raises(ValueError):
        true_range(np.zeros(3), np.zeros(4), np.zeros(3))
    with pytest.raises(ValueError):
        donchian(np.zeros(3), np.zeros(4), 2)
    with pytest.raises(ValueError):
        adx(np.zeros(5), np.zeros(5), np.zeros(5), 0)
