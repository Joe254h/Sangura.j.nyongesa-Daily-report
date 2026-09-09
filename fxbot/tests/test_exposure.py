"""Portfolio exposure tests (§8.4).

Long EURUSD and long GBPUSD are both **short USD**: one trade wearing two tickets, which
is exactly what the cluster cap exists to stop.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from tests.conftest import SERVER_TZ, make_position

from fxbot.core.enums import RejectReason, Side
from fxbot.core.models import SizedOrder
from fxbot.risk.exposure import check_exposure, cluster_of, usd_direction
from fxbot.risk.sizing import position_risk_amount, position_size

WHEN = datetime(2024, 6, 3, 10, tzinfo=SERVER_TZ)
COMMISSION = 7.00


def candidate(symbol: str, side: Side = Side.BUY, risk: float = 50.0) -> SizedOrder:
    """A sized order awaiting the exposure verdict."""
    return SizedOrder(symbol=symbol, side=side, volume=0.10, stop_price=1.07,
                      take_profit=None, risk_amount=risk, risk_pct=risk / 100.0,
                      approval_id="test")


def test_usd_direction_reads_both_legs(specs) -> None:
    """Base USD and profit USD are mirror images; a symbol with no USD leg is unclustered."""
    assert usd_direction(specs["EURUSD"], Side.BUY) == -1
    assert usd_direction(specs["EURUSD"], Side.SELL) == 1
    assert usd_direction(specs["USDJPY"], Side.BUY) == 1
    assert usd_direction(specs["USDJPY"], Side.SELL) == -1
    eurgbp = replace(specs["EURUSD"], name="EURGBP", currency_base="EUR",
                     currency_profit="GBP")
    assert usd_direction(eurgbp, Side.BUY) == 0


def test_long_eurusd_and_long_gbpusd_are_one_short_usd_cluster(cfg, specs) -> None:
    """Two tickets, one trade. The third is refused with CLUSTER_LIMIT (§12.3)."""
    open_positions = [
        make_position(specs["EURUSD"], Side.BUY, 1.0800, 1.0760, WHEN, ticket=1),
        make_position(specs["GBPUSD"], Side.BUY, 1.2600, 1.2550, WHEN, ticket=2),
    ]
    for position in open_positions:
        assert usd_direction(specs[position.symbol], position.side) == -1
    assert cluster_of("AUDUSD", cfg.risk.clusters) == "USD_SHORT_BLOC"

    reason = check_exposure(candidate("AUDUSD"), open_positions, specs, 10_000.0,
                            cfg.risk, COMMISSION)
    assert reason is RejectReason.CLUSTER_LIMIT


def test_opposite_usd_direction_in_the_same_cluster_does_not_count(cfg, specs) -> None:
    """Two longs and one short EURUSD are not three of the same trade."""
    open_positions = [
        make_position(specs["EURUSD"], Side.SELL, 1.0800, 1.0840, WHEN, ticket=1),
        make_position(specs["GBPUSD"], Side.SELL, 1.2600, 1.2650, WHEN, ticket=2),
    ]
    reason = check_exposure(candidate("AUDUSD", Side.BUY), open_positions, specs,
                            10_000.0, cfg.risk, COMMISSION)
    assert reason is RejectReason.NONE


def test_max_open_positions_is_checked_first(cfg, specs) -> None:
    """Order matters: MAX_POSITIONS -> SYMBOL_ALREADY_OPEN -> CLUSTER -> TOTAL_RISK."""
    positions = [
        make_position(specs["EURUSD"], Side.BUY, 1.08, 1.076, WHEN, ticket=1),
        make_position(specs["USDJPY"], Side.BUY, 157.0, 156.5, WHEN, ticket=2),
        make_position(specs["USDCAD"], Side.SELL, 1.36, 1.365, WHEN, ticket=3),
    ]
    assert check_exposure(candidate("AUDUSD"), positions, specs, 10_000.0, cfg.risk,
                          COMMISSION) is RejectReason.MAX_POSITIONS


def test_symbol_already_open_is_defence_in_depth(cfg, specs) -> None:
    """It looks unreachable, and it catches a stale positions list. Tested, not deleted."""
    positions = [make_position(specs["EURUSD"], Side.BUY, 1.08, 1.076, WHEN, ticket=1)]
    assert check_exposure(candidate("EURUSD"), positions, specs, 10_000.0, cfg.risk,
                          COMMISSION) is RejectReason.SYMBOL_ALREADY_OPEN


def test_total_risk_cap_includes_commission_and_matches_sizing(cfg, specs) -> None:
    """The commission term is not optional: both layers must agree on "0.5% risk" (§8.4)."""
    spec = specs["EURUSD"]
    sized = position_size(10_000.0, 0.5, 1.08500, 1.08320, spec, COMMISSION)
    position = make_position(spec, Side.BUY, 1.08500, 1.08320, WHEN, volume=sized.volume,
                             ticket=1)
    open_risk = position_risk_amount(position.volume, position.entry_price,
                                     position.stop_loss, spec, COMMISSION)
    assert open_risk == pytest.approx(sized.risk_amount, abs=0.005)

    # 1.5% cap on $10,000 is $150. One position at ~$48.62 plus a $110 candidate exceeds it.
    assert check_exposure(candidate("GBPUSD", risk=110.0), [position], specs, 10_000.0,
                          cfg.risk, COMMISSION) is RejectReason.TOTAL_RISK_CAP
    assert check_exposure(candidate("GBPUSD", risk=90.0), [position], specs, 10_000.0,
                          cfg.risk, COMMISSION) is RejectReason.NONE


def test_an_unknown_spec_fails_closed(cfg, specs) -> None:
    """Uncertainty halts entries; it does not wave them through (§0.7)."""
    assert check_exposure(candidate("XAUUSD"), [], specs, 10_000.0, cfg.risk,
                          COMMISSION) is RejectReason.TOTAL_RISK_CAP
    orphan = make_position(specs["EURUSD"], Side.BUY, 1.08, 1.076, WHEN, ticket=1)
    orphan = replace(orphan, symbol="UNKNOWN")
    assert check_exposure(candidate("GBPUSD"), [orphan], {"GBPUSD": specs["GBPUSD"]},
                          10_000.0, cfg.risk, COMMISSION) is RejectReason.TOTAL_RISK_CAP


def test_non_positive_equity_refuses(cfg, specs) -> None:
    """Failure path: a blown account takes no more risk."""
    assert check_exposure(candidate("EURUSD"), [], specs, 0.0, cfg.risk,
                          COMMISSION) is RejectReason.TOTAL_RISK_CAP


def test_a_clean_candidate_passes_every_rule(cfg, specs) -> None:
    """The happy path returns NONE."""
    assert check_exposure(candidate("EURUSD"), [], specs, 10_000.0, cfg.risk,
                          COMMISSION) is RejectReason.NONE


def test_cluster_lookup_returns_none_for_an_unclustered_symbol(cfg) -> None:
    """A symbol outside every cluster is never capped by one."""
    assert cluster_of("XAUUSD", cfg.risk.clusters) is None
    assert cluster_of("EURUSD", {}) is None


def test_positions_in_another_cluster_do_not_count(cfg, specs) -> None:
    """The USD_LONG cluster's members must not consume the USD_SHORT cap."""
    positions = [make_position(specs["USDJPY"], Side.BUY, 157.0, 156.5, WHEN, ticket=1)]
    reason = check_exposure(candidate("EURUSD", Side.BUY), positions, specs, 10_000.0,
                            cfg.risk, COMMISSION)
    assert reason is RejectReason.NONE


def test_a_position_with_no_spec_is_skipped_by_the_cluster_scan(cfg, specs) -> None:
    """A missing spec cannot be classified long or short USD, so it is not counted there."""
    positions = [replace(make_position(specs["EURUSD"], Side.BUY, 1.08, 1.076, WHEN,
                                       ticket=1), symbol="UNKNOWN")]
    partial = {k: v for k, v in specs.items() if k != "UNKNOWN"}
    assert check_exposure(candidate("GBPUSD"), positions, partial, 10_000.0, cfg.risk,
                          COMMISSION) is RejectReason.TOTAL_RISK_CAP
