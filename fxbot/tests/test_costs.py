"""Shared fill-model tests (§11.2, §12.5).

Understated costs are how a losing H1 breakout system looks profitable. These tests pin
each cost down separately so no single one can quietly go missing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from tests.conftest import SERVER_TZ

from fxbot.backtest.costs import AccountBook, FillModel, floating_pnl, realised_pnl
from fxbot.core.enums import Side
from fxbot.core.models import Bar

WHEN = datetime(2024, 6, 3, 10, tzinfo=SERVER_TZ)


def model(spec, **kwargs):  # noqa: ANN001, ANN201
    """A fill model over ``spec`` with Razor defaults."""
    defaults = {"commission_per_lot_per_side": 3.50, "slippage_points": 3,
                "spread_source": "historical", "fixed_spread_points": 8}
    return FillModel(spec=spec, **{**defaults, **kwargs})


def bar(open_: float, high: float, low: float, close: float, spread: int = 10) -> Bar:
    """One bar."""
    return Bar(time=WHEN, open=open_, high=high, low=low, close=close, volume=100,
               spread=spread)


def test_slippage_is_converted_from_points_to_price(eurusd) -> None:
    """Backtrader's ``fixed`` argument is absolute price units, not points (§11.2).

    Passing a raw 3 slips every EURUSD fill by 3.00 -- 100,000x too much -- and silently
    invalidates every result and therefore the §11.4 gate.
    """
    assert model(eurusd).slippage_price() == pytest.approx(3 * 1e-5)
    assert model(eurusd).slippage_price() != 3.0


def test_bars_are_bid_so_only_buy_side_pays_the_spread(eurusd) -> None:
    """A long entry and a short exit both buy, and both pay. Sells do not."""
    m = model(eurusd)
    b = bar(1.08000, 1.08200, 1.07900, 1.08100, spread=10)
    buy = m.market_fill(Side.BUY, b)
    sell = m.market_fill(Side.SELL, b)
    assert buy == pytest.approx(1.08000 + (10 + 3) * 1e-5)
    assert sell == pytest.approx(1.08000 - 3 * 1e-5)
    assert buy - sell == pytest.approx((10 + 6) * 1e-5)


def test_market_orders_fill_at_the_next_bars_open_never_the_close(eurusd) -> None:
    """The live engine decides after the close and sends immediately; so does this."""
    m = model(eurusd)
    b = bar(1.08000, 1.09000, 1.07000, 1.08800)
    assert m.market_fill(Side.SELL, b) == pytest.approx(1.08000 - 3e-5)
    assert m.market_fill(Side.SELL, b) != pytest.approx(b.close)


def test_a_stop_that_is_not_touched_does_not_fill(eurusd) -> None:
    """No trigger, no fill, and no silent close."""
    m = model(eurusd)
    assert m.stop_fill(Side.BUY, bar(1.0800, 1.0820, 1.0790, 1.0810), 1.0700) is None
    assert m.stop_fill(Side.SELL, bar(1.0800, 1.0820, 1.0790, 1.0810), 1.0900) is None
    assert m.stop_fill(Side.BUY, bar(1.0800, 1.0820, 1.0790, 1.0810), 0.0) is None


def test_a_stop_touched_intrabar_fills_at_the_stop_price(eurusd) -> None:
    """Inside the bar the trigger price is the fill, minus adverse slippage."""
    m = model(eurusd)
    fill = m.stop_fill(Side.BUY, bar(1.0800, 1.0820, 1.0750, 1.0810), 1.0770)
    assert fill == pytest.approx(1.0770 - 3e-5)


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop(eurusd) -> None:
    """This is where weekend gaps actually cost money (§11.5, §12.5)."""
    m = model(eurusd)
    gapped = bar(1.0700, 1.0720, 1.0680, 1.0710)
    fill = m.stop_fill(Side.BUY, gapped, 1.0770)
    assert fill == pytest.approx(1.0700 - 3e-5)
    assert fill < 1.0770 - 3e-5, "a gap fill is worse than the stop, not equal to it"


def test_a_short_stop_gap_fills_at_the_open_and_pays_the_spread(eurusd) -> None:
    """Buying back a short pays the ask, gap or no gap."""
    m = model(eurusd)
    gapped = bar(1.0900, 1.0920, 1.0880, 1.0910, spread=12)
    fill = m.stop_fill(Side.SELL, gapped, 1.0850)
    assert fill == pytest.approx(1.0900 + (12 + 3) * 1e-5)


def test_commission_is_charged_per_side(eurusd) -> None:
    """USD 3.50 per lot per side; USD 7.00 round turn on a standard lot."""
    m = model(eurusd)
    assert m.commission(1.0) == pytest.approx(3.50)
    assert m.commission(0.26) == pytest.approx(0.91)
    assert 2 * m.commission(1.0) == pytest.approx(7.00)


def test_the_stress_multipliers_widen_spread_and_slippage(eurusd) -> None:
    """§11.2's 1.5x spread / 2x slippage run, which the edge has to survive."""
    stressed = model(eurusd, spread_multiplier=1.5, slippage_multiplier=2.0)
    assert stressed.spread_price(10) == pytest.approx(10 * 1e-5 * 1.5)
    assert stressed.slippage_price() == pytest.approx(3 * 1e-5 * 2.0)


def test_fixed_spread_source_ignores_the_bar_column(eurusd) -> None:
    """``spread_source: fixed`` is for histories with no usable spread column."""
    fixed = model(eurusd, spread_source="fixed", fixed_spread_points=8)
    assert fixed.spread_price(50) == pytest.approx(8 * 1e-5)


def test_swap_is_charged_per_rollover_and_tripled_into_thursday(eurusd) -> None:
    """H1 holds last days; swap is not optional (§11.5, §17.17)."""
    m = model(eurusd)
    tuesday = datetime(2024, 6, 4, 10, tzinfo=SERVER_TZ)
    assert FillModel.swap_nights(tuesday, tuesday + timedelta(days=1)) == 1
    # Tue -> Fri crosses Wed(1), Thu(3), Fri(1).
    assert FillModel.swap_nights(tuesday, tuesday + timedelta(days=3)) == 5
    # Fri -> Mon crosses Sat(0), Sun(0), Mon(1): the triple Wednesday already paid.
    friday = datetime(2024, 6, 7, 10, tzinfo=SERVER_TZ)
    assert FillModel.swap_nights(friday, friday + timedelta(days=3)) == 1
    assert FillModel.swap_nights(tuesday, tuesday) == 0

    cost = m.swap(Side.BUY, 1.0, tuesday, tuesday + timedelta(days=1))
    assert cost == pytest.approx(eurusd.swap_long * eurusd.point
                                 * eurusd.value_per_price_unit_per_lot)
    assert cost < 0.0, "a long EURUSD swap is a cost on this fixture"


def test_an_unmodelled_swap_mode_returns_zero_and_the_runner_refuses(eurusd) -> None:
    """Returning 0.0 would understate costs, so the runner refuses to report at all."""
    from fxbot.backtest.runner import assert_swaps_modelled
    from fxbot.core.errors import ConfigError

    exotic = replace(eurusd, swap_mode=1)
    assert model(exotic).swap(Side.BUY, 1.0, WHEN, WHEN + timedelta(days=3)) == 0.0
    with pytest.raises(ConfigError, match="swap_mode"):
        assert_swaps_modelled({"EURUSD": exotic})
    assert_swaps_modelled({"EURUSD": eurusd})


def test_pnl_helpers_scale_by_the_symbols_own_tick_value(eurusd, usdjpy) -> None:
    """Never "$10 a pip": JPY crosses have a different value per price unit (§6.2)."""
    assert realised_pnl(Side.BUY, 1.0, 1.0800, 1.0810, eurusd) == pytest.approx(100.0)
    assert realised_pnl(Side.SELL, 1.0, 1.0800, 1.0810, eurusd) == pytest.approx(-100.0)
    assert floating_pnl(Side.SELL, 0.5, 157.000, 156.500, usdjpy) == pytest.approx(
        0.5 * 0.5 * usdjpy.value_per_price_unit_per_lot)


def test_the_account_book_tracks_balance_and_equity() -> None:
    """Both engines are handed their equity by this one class, so it cannot drift (§12.5)."""
    book = AccountBook(10_000.0)
    book.charge(0.91)
    book.credit(48.0)
    assert book.balance == pytest.approx(10_047.09)
    assert book.equity(-12.5) == pytest.approx(10_034.59)
