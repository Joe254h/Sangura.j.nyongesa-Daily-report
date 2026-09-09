"""Position-sizing tests (§12.3).

The most test-covered function in the repo, because a bot that risks more than intended
costs the account (§18). Every rounding decision is asserted to bias downward.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fxbot.core.enums import RejectReason
from fxbot.core.errors import SizingError
from fxbot.core.models import SymbolSpec
from fxbot.risk.sizing import position_risk_amount, position_size

COMMISSION_ROUND_TURN = 7.00
"""Pepperstone Razor MT5: USD 3.50 per lot per side."""


def test_the_spec_worked_example_exactly(eurusd: SymbolSpec) -> None:
    """§8.2's worked example, asserted to the cent.

    Equity $10,000, risk 0.5%, entry 1.08500, stop 1.08320 (2 x ATR of 0.00090):
    value/unit/lot 100,000; budget $50.00; cost/lot $187.00; raw 0.26738 -> 0.26 lots;
    risk $48.62, which is 0.486% of equity.
    """
    result = position_size(10_000.0, 0.5, 1.08500, 1.08320, eurusd, COMMISSION_ROUND_TURN)
    assert result.volume == pytest.approx(0.26)
    assert result.risk_amount == pytest.approx(48.62, abs=0.005)
    assert 0.45 <= result.risk_pct <= 0.50
    assert result.reason is RejectReason.NONE


def test_an_account_too_small_for_the_stop_refuses_to_trade(eurusd: SymbolSpec) -> None:
    """Below ``volume_min`` is a refusal, never a round-up to the minimum lot (§17.7)."""
    result = position_size(200.0, 0.5, 1.08500, 1.08320, eurusd, COMMISSION_ROUND_TURN)
    assert result.volume == 0.0
    assert result.reason is RejectReason.SIZE_BELOW_MIN
    assert "0.0053" in result.detail


def test_commission_is_inside_the_denominator(eurusd: SymbolSpec) -> None:
    """Excluding commission systematically oversizes (§8.2 step 4)."""
    with_commission = position_size(10_000.0, 0.5, 1.08500, 1.08320, eurusd, 7.0)
    without = position_size(10_000.0, 0.5, 1.08500, 1.08320, eurusd, 0.0)
    assert without.volume > with_commission.volume


def test_rounding_is_always_down_never_up(eurusd: SymbolSpec) -> None:
    """The volume is the largest step multiple at or below the raw size."""
    for equity in (3_337.0, 7_919.0, 12_345.67, 98_765.43):
        result = position_size(equity, 0.5, 1.08500, 1.08320, eurusd, COMMISSION_ROUND_TURN)
        if result.volume == 0.0:
            continue
        raw = (equity * 0.005) / (0.00180 * 100_000 + COMMISSION_ROUND_TURN)
        assert result.volume <= raw + 1e-12
        assert raw - result.volume < eurusd.volume_step


def test_float_dust_does_not_cross_a_step_boundary_upward(eurusd: SymbolSpec) -> None:
    """``floor(x + 1e-9)`` would cross the boundary; rounding the ratio to 9dp does not."""
    assert eurusd.floor_volume(0.30000000000000004) == pytest.approx(0.30)
    assert eurusd.floor_volume(0.2999999999) == pytest.approx(0.29)
    assert eurusd.floor_volume(0.26999999999999996) == pytest.approx(0.27)


def test_three_digit_jpy_symbol_sizes_correctly(usdjpy: SymbolSpec) -> None:
    """Never compute pips by dividing by a hardcoded 10000 (§6.2)."""
    assert usdjpy.digits == 3
    result = position_size(10_000.0, 0.5, 157.200, 156.900, usdjpy, COMMISSION_ROUND_TURN)
    value_per_unit = usdjpy.tick_value / usdjpy.tick_size
    cost_per_lot = 0.300 * value_per_unit + COMMISSION_ROUND_TURN
    assert result.volume == pytest.approx(usdjpy.floor_volume(50.0 / cost_per_lot))
    assert result.risk_amount <= 50.0


def test_reduced_status_halves_the_size(eurusd: SymbolSpec) -> None:
    """The governor's size multiplier scales the budget, not the stop."""
    full = position_size(10_000.0, 0.5, 1.08500, 1.08320, eurusd, COMMISSION_ROUND_TURN)
    half = position_size(10_000.0, 0.5, 1.08500, 1.08320, eurusd, COMMISSION_ROUND_TURN, 0.5)
    assert half.volume == pytest.approx(0.13)
    assert half.volume <= full.volume / 2 + eurusd.volume_step


def test_zero_stop_distance_is_refused(eurusd: SymbolSpec) -> None:
    """A stop at the entry is not an infinite position."""
    result = position_size(10_000.0, 0.5, 1.08500, 1.08500, eurusd, COMMISSION_ROUND_TURN)
    assert result.volume == 0.0
    assert result.reason is RejectReason.SIZE_BELOW_MIN


def test_non_positive_equity_is_refused(eurusd: SymbolSpec) -> None:
    """Failure path: a blown account does not get a position."""
    assert position_size(0.0, 0.5, 1.085, 1.083, eurusd, 7.0).volume == 0.0
    assert position_size(-5.0, 0.5, 1.085, 1.083, eurusd, 7.0).volume == 0.0
    assert position_size(10_000.0, 0.5, 1.085, 1.083, eurusd, 7.0, 0.0).volume == 0.0


def test_unusable_tick_value_raises_rather_than_guessing(eurusd: SymbolSpec) -> None:
    """Never guess a pip value: ``$10/pip`` is wrong for most symbols (§6.2)."""
    from dataclasses import replace

    with pytest.raises(SizingError):
        position_size(10_000.0, 0.5, 1.085, 1.083, replace(eurusd, tick_value=0.0), 7.0)
    with pytest.raises(SizingError):
        position_size(10_000.0, 0.5, 1.085, 1.083, replace(eurusd, tick_size=0.0), 7.0)


def test_volume_is_capped_at_the_broker_maximum(eurusd: SymbolSpec) -> None:
    """A very large account still cannot exceed ``volume_max``."""
    from dataclasses import replace

    capped = replace(eurusd, volume_max=1.0)
    result = position_size(10_000_000.0, 0.5, 1.08500, 1.08320, capped, COMMISSION_ROUND_TURN)
    assert result.volume == pytest.approx(1.0)


def test_position_risk_amount_matches_sizing_to_the_cent(eurusd: SymbolSpec) -> None:
    """``exposure.py`` and ``sizing.py`` must agree on what 0.5% risk means (§8.4)."""
    sized = position_size(10_000.0, 0.5, 1.08500, 1.08320, eurusd, COMMISSION_ROUND_TURN)
    recomputed = position_risk_amount(sized.volume, 1.08500, 1.08320, eurusd,
                                      COMMISSION_ROUND_TURN)
    assert recomputed == pytest.approx(sized.risk_amount, abs=0.005)


def test_position_risk_amount_is_zero_without_a_stop(eurusd: SymbolSpec) -> None:
    """A position with no stop reports zero risk; the caller treats that as a problem."""
    assert position_risk_amount(0.5, 1.085, 0.0, eurusd, 7.0) == 0.0


@settings(max_examples=250, deadline=None)
@given(
    equity=st.floats(min_value=500.0, max_value=5_000_000.0, allow_nan=False),
    risk_pct=st.floats(min_value=0.05, max_value=3.0, allow_nan=False),
    entry=st.floats(min_value=0.5, max_value=2.0, allow_nan=False),
    distance=st.floats(min_value=0.0002, max_value=0.02, allow_nan=False),
    multiplier=st.sampled_from([0.5, 1.0]),
)
def test_property_risk_never_exceeds_the_budget(equity: float, risk_pct: float, entry: float,
                                                distance: float, multiplier: float) -> None:
    """For any valid input, ``risk_amount <= risk_budget`` and volume is a step multiple."""
    spec = SymbolSpec(
        name="EURUSD", digits=5, point=1e-5, tick_size=1e-5, tick_value=1.0,
        tick_value_profit=1.0, contract_size=100_000.0, swap_long=-7.0, swap_short=1.0,
        swap_mode=0, trade_exemode=2, currency_base="EUR", volume_min=0.01,
        volume_max=100.0, volume_step=0.01, stops_level=0, freeze_level=0,
        filling_modes=2, currency_profit="USD", currency_margin="EUR",
    )
    result = position_size(equity, risk_pct, entry, entry - distance, spec,
                           COMMISSION_ROUND_TURN, multiplier)
    budget = equity * risk_pct / 100.0 * multiplier
    assert result.risk_amount <= budget + 1e-9
    steps = result.volume / spec.volume_step
    assert math.isclose(steps, round(steps), abs_tol=1e-6)


def test_a_risk_budget_that_underflows_to_zero_refuses(eurusd: SymbolSpec) -> None:
    """Failure path: a budget that underflows to nothing is refused before the division."""
    result = position_size(5e-324, 0.5, 1.08500, 1.08320, eurusd, COMMISSION_ROUND_TURN)
    assert result.volume == 0.0
    assert result.reason is RejectReason.SIZE_BELOW_MIN
    assert "risk budget" in result.detail

    tiny = position_size(1e-9, 0.5, 1.08500, 1.08320, eurusd, COMMISSION_ROUND_TURN)
    assert tiny.volume == 0.0


def test_a_negative_commission_is_refused_rather_than_used(eurusd: SymbolSpec) -> None:
    """A cost per lot at or below zero means the inputs are wrong; never divide by it."""
    with pytest.raises(SizingError, match="cost per lot"):
        position_size(10_000.0, 0.5, 1.08500, 1.08320, eurusd, -1_000.0)


def test_an_infinite_stop_distance_is_refused(eurusd: SymbolSpec) -> None:
    """Non-finite inputs never reach the division."""
    result = position_size(10_000.0, 0.5, float("inf"), 1.08320, eurusd,
                           COMMISSION_ROUND_TURN)
    assert result.volume == 0.0
    assert result.reason is RejectReason.SIZE_BELOW_MIN


def test_the_shared_price_helpers_agree_with_the_spec_methods(eurusd: SymbolSpec) -> None:
    """``strategy/base.py``'s wrappers exist for readability, not to be a second copy."""
    from fxbot.core.enums import Side
    from fxbot.strategy.base import (
        enforce_stop_distance,
        floor_to_step,
        min_stop_distance,
        partial_close_volume,
        round_price,
        round_stop_away,
    )

    assert round_price(1.085004, 5) == pytest.approx(1.08500)
    assert round_stop_away(1.0832049, 1.0850, eurusd.digits) == pytest.approx(
        eurusd.round_stop_away(1.0832049, 1.0850))
    assert round_stop_away(1.0870049, 1.0850, eurusd.digits) == pytest.approx(1.08701)
    assert round_stop_away(1.0850, 1.0850, eurusd.digits) == pytest.approx(1.0850)
    assert min_stop_distance(eurusd, 10) == pytest.approx(eurusd.min_stop_distance(10))
    assert floor_to_step(0.2678, 0.01) == pytest.approx(eurusd.floor_volume(0.2678))
    assert partial_close_volume(0.23, 0.5, eurusd) == pytest.approx(0.11)

    from dataclasses import replace

    wide = replace(eurusd, stops_level=200)
    long_stop = enforce_stop_distance(1.08490, 1.08500, Side.BUY, wide, 10)
    assert 1.08500 - long_stop >= wide.min_stop_distance(10) - 1e-12
    short_stop = enforce_stop_distance(1.08510, 1.08500, Side.SELL, wide, 10)
    assert short_stop - 1.08500 >= wide.min_stop_distance(10) - 1e-12

    with pytest.raises(ValueError, match="volume_step"):
        floor_to_step(1.0, 0.0)
    with pytest.raises(ValueError, match="volume_step"):
        replace(eurusd, volume_step=0.0).floor_volume(1.0)
