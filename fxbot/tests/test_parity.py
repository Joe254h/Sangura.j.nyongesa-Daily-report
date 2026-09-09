"""The keystone test (§12.5).

Runs 2,000 bars of fixture data through (a) the Backtrader harness and (b) the live
:class:`~fxbot.runtime.engine.TradingEngine` driven by
:class:`~fxbot.backtest.replay.ReplayDataSource` plus
:class:`~fxbot.execution.paper_broker.PaperBroker`, and asserts identical trade count,
identical entry bars, identical sides, and entry/exit prices within 1e-9.

**Any divergence fails the build.** This test is the reason the architecture is shaped the
way it is; do not weaken it to make it pass. If the two engines disagree, the answer is to
find which one is wrong -- not to loosen the tolerance.
"""

from __future__ import annotations

import pytest
from tests.conftest import load_bars, load_spec

from fxbot.backtest.runner import run_backtrader
from fxbot.core.clock import ServerClock
from fxbot.risk.governor import RiskGovernor
from fxbot.runtime.engine import replay_history
from fxbot.runtime.journal import Journal

PRICE_TOLERANCE = 1e-9
"""§12.5's tolerance. Both engines share one fill model, so this is achievable."""

STARTING_EQUITY = 10_000.0
BARS = 2_200
"""§12.5 asks for 2,000 bars; the whole fixture is used instead.

The extra 200 bars are not padding. Two real divergences only appeared past bar 2,000 --
an expanding-vs-rolling context window that let the two engines' recursive indicators
drift apart, and a Backtrader fill stamped one bar after the bar it priced from. A test
that stops at 2,000 would have passed through both.
"""


@pytest.fixture(scope="module")
def parity_frames():  # noqa: ANN201
    """The two fixture histories, truncated to the 2,000 bars §12.5 specifies."""
    return {
        "EURUSD": load_bars("parity_eurusd").iloc[:BARS],
        "GBPUSD": load_bars("parity_gbpusd").iloc[:BARS],
    }


@pytest.fixture(scope="module")
def parity_specs():  # noqa: ANN201
    """The captured specifications for the two parity symbols."""
    return {"EURUSD": load_spec("EURUSD"), "GBPUSD": load_spec("GBPUSD")}


def run_both(cfg, frames, specs, tmp_path):  # noqa: ANN001, ANN201
    """Run the same bars through both engines and return ``(backtrader, replay)``."""
    clock_bt = ServerClock(3)
    clock_bt.observe(frames["EURUSD"].index[0].to_pydatetime())
    clock_replay = ServerClock(3)
    clock_replay.observe(frames["EURUSD"].index[0].to_pydatetime())

    bt_journal = Journal(tmp_path / "bt.db")
    replay_journal = Journal(tmp_path / "replay.db")
    try:
        bt_governor = RiskGovernor(cfg, tmp_path / "bt_risk.json", clock_bt, bt_journal)
        bt_governor.load()
        bt_governor.set_symbol_specs(specs)
        backtrader_run = run_backtrader(cfg, clock_bt, frames, specs, bt_governor,
                                        STARTING_EQUITY)

        def factory(sink=replay_journal):  # noqa: ANN001, ANN202
            governor = RiskGovernor(cfg, tmp_path / "replay_risk.json", clock_replay, sink)
            governor.load()
            governor.set_symbol_specs(specs)
            return governor

        from fxbot.ops.alerts import Alerter

        replay_run = replay_history(cfg, clock_replay, frames, specs, factory,
                                    replay_journal,
                                    Alerter(False, "CRITICAL", None, None, "test"),
                                    STARTING_EQUITY)
    finally:
        bt_journal.close()
        replay_journal.close()
    return backtrader_run, replay_run


@pytest.fixture(scope="module")
def runs(parity_frames, parity_specs, tmp_path_factory):  # noqa: ANN201
    """Both engine runs, computed once for the whole module.

    The 2,000-bar run is not cheap, so it happens once and every assertion below reads the
    same pair of results.
    """
    from fxbot.config.schema import AppConfig

    return run_both(AppConfig(env="backtest"), parity_frames, parity_specs,
                    tmp_path_factory.mktemp("parity"))


def test_the_run_actually_traded(runs) -> None:
    """A parity test over zero trades proves nothing."""
    backtrader_run, replay_run = runs
    assert backtrader_run.trades, "the Backtrader engine took no trades on the fixture"
    assert replay_run.trades, "the replay engine took no trades on the fixture"


def test_identical_trade_count(runs) -> None:
    """The two engines take the same number of trades."""
    backtrader_run, replay_run = runs
    assert len(backtrader_run.trades) == len(replay_run.trades)


def test_identical_entry_bars_and_sides(runs) -> None:
    """Every trade opens on the same bar, in the same direction, on the same symbol."""
    backtrader_run, replay_run = runs
    left = [(t.symbol, t.side, t.entry_time) for t in backtrader_run.trades]
    right = [(t.symbol, t.side, t.entry_time) for t in replay_run.trades]
    assert left == right


def test_identical_exit_bars_and_reasons(runs) -> None:
    """Exits agree too: same bar, same reason. A stop and a trail are not interchangeable."""
    backtrader_run, replay_run = runs
    left = [(t.exit_time, t.exit_reason) for t in backtrader_run.trades]
    right = [(t.exit_time, t.exit_reason) for t in replay_run.trades]
    assert left == right


def test_entry_and_exit_prices_agree_to_one_part_in_a_billion(runs) -> None:
    """§12.5's 1e-9. Both engines price fills with the one model in ``backtest/costs.py``."""
    backtrader_run, replay_run = runs
    for left, right in zip(backtrader_run.trades, replay_run.trades, strict=True):
        assert left.entry_price == pytest.approx(right.entry_price, abs=PRICE_TOLERANCE)
        assert left.exit_price == pytest.approx(right.exit_price, abs=PRICE_TOLERANCE)


def test_volumes_and_pnl_agree(runs) -> None:
    """Same size and same money: the governor saw the same equity in both engines."""
    backtrader_run, replay_run = runs
    for left, right in zip(backtrader_run.trades, replay_run.trades, strict=True):
        assert left.volume == pytest.approx(right.volume, abs=1e-9)
        assert left.net_pnl == pytest.approx(right.net_pnl, abs=1e-6)
        assert left.commission == pytest.approx(right.commission, abs=1e-9)
        assert left.swap == pytest.approx(right.swap, abs=1e-9)


def test_the_reject_histograms_agree(runs) -> None:
    """The same bars were refused for the same reasons -- the decision path is shared too."""
    backtrader_run, replay_run = runs
    assert backtrader_run.reject_histogram == replay_run.reject_histogram
