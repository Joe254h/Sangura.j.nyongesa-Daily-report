"""Backtest runners (§11).

Two engines, one strategy:

* :func:`run_backtrader` drives the pure functions through Backtrader (§11.1);
* :func:`fxbot.runtime.engine.replay_history` drives the **live**
  :class:`~fxbot.runtime.engine.TradingEngine` over the same bars.

``tests/test_parity.py`` asserts they agree to 1e-9.

The replay half deliberately lives in ``runtime/`` and not here: §2.1 forbids ``backtest/``
from importing ``runtime.engine``, and driving the live engine over a history is a runtime
concern that happens to be useful for research -- not a Backtrader concern.
``tests/test_layering.py`` is what caught the arrow pointing the wrong way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

import backtrader as bt
import pandas as pd

from fxbot.backtest.bt_strategy import BtRunState, FxStrategy
from fxbot.backtest.costs import AccountBook
from fxbot.backtest.feeds import CostBroker, FxCommission, make_feed
from fxbot.backtest.metrics import (
    RunResult,
    assert_swaps_modelled,
    build_models,
    build_report,
    narrow_to,
)
from fxbot.config.schema import AppConfig
from fxbot.core.clock import ServerClock
from fxbot.core.models import SymbolSpec
from fxbot.risk.governor import RiskGovernor


def run_backtrader(
    cfg: AppConfig,
    clock: ServerClock,
    frames: Mapping[str, pd.DataFrame],
    specs: Mapping[str, SymbolSpec],
    governor: RiskGovernor,
    starting_equity: float = 10_000.0,
    spread_multiplier: float = 1.0,
    slippage_multiplier: float = 1.0,
) -> RunResult:
    """Run the Backtrader engine over ``frames``.

    Args:
        cfg: The resolved configuration.
        clock: The broker clock.
        frames: ``symbol -> ascending bar frame``.
        specs: ``symbol -> SymbolSpec``, captured from the live broker.
        governor: A loaded governor; the real kill switch runs in the backtest too.
        starting_equity: Opening balance.
        spread_multiplier: §11.2 stress knob.
        slippage_multiplier: §11.2 stress knob.

    Returns:
        The :class:`RunResult`.
    """
    assert_swaps_modelled(specs)
    cfg = narrow_to(cfg, list(frames))
    models = build_models(cfg, specs, spread_multiplier, slippage_multiplier)
    book = AccountBook(starting_equity)
    state = BtRunState()

    cerebro = bt.Cerebro(stdstats=False)
    broker = CostBroker()
    broker.set_fill_models(models)
    broker.setcash(starting_equity * 1000.0)
    # Signals fire on a closed bar; fills occur at the next bar's open. This matches the
    # live engine, which decides after the close and sends a market order immediately.
    broker.set_coc(False)
    # Cash is not the constraint being tested -- the RiskGovernor is. Letting Backtrader
    # reject on margin would silently replace the risk model under test with its own.
    broker.set_checksubmit(False)
    cerebro.setbroker(broker)

    for symbol, frame in frames.items():
        feed = make_feed(frame, symbol)
        cerebro.adddata(feed, name=symbol)
        spec = specs[symbol]
        cerebro.broker.addcommissioninfo(
            FxCommission(commission=cfg.costs.commission_per_lot_per_side,
                         mult=spec.value_per_price_unit_per_lot),
            name=symbol,
        )

    cerebro.addstrategy(FxStrategy, cfg=cfg, clock=clock, specs=specs, frames=frames,
                        governor=governor, book=book, state=state, models=models)
    cerebro.run(runonce=False, preload=True)

    histogram: dict[str, int] = {}
    regimes: dict[tuple[str, datetime], str] = {}
    for record in state.decisions:
        histogram[record.reason] = histogram.get(record.reason, 0) + 1
        if record.side is not None:
            regimes[(record.symbol, record.when)] = record.regime

    report = build_report(state.trades, state.equity_curve, starting_equity,
                          cfg.timeframe_minutes, histogram, regimes)
    return RunResult(state.trades, state.equity_curve, report, histogram)


def cost_sensitivity(
    cfg: AppConfig,
    clock: ServerClock,
    frames: Mapping[str, pd.DataFrame],
    specs: Mapping[str, SymbolSpec],
    governor_factory: Callable[[], RiskGovernor],
    starting_equity: float = 10_000.0,
    multipliers: Sequence[tuple[str, float, float]] = (
        ("1x", 1.0, 1.0), ("1.5x", 1.5, 2.0), ("2x", 2.0, 3.0),
    ),
) -> dict[str, float]:
    """Return net profit at 1x, 1.5x and 2x costs (§11.2, §11.3).

    The stress run is not decoration: if the edge dies at 1.5x spread and 2x slippage it
    is not an edge, because real fills at H1 breakouts on news-adjacent bars are worse
    than the average.

    Args:
        cfg: The resolved configuration.
        clock: The broker clock.
        frames: ``symbol -> ascending bar frame``.
        specs: ``symbol -> SymbolSpec``.
        governor_factory: Callable returning a fresh loaded governor.
        starting_equity: Opening balance.
        multipliers: ``(label, spread multiple, slippage multiple)`` triples.

    Returns:
        ``label -> net profit``.
    """
    out: dict[str, float] = {}
    for label, spread_mult, slip_mult in multipliers:
        result = run_backtrader(cfg, clock, frames, specs, governor_factory(),
                                starting_equity, spread_mult, slip_mult)
        out[label] = result.report.net_profit
    return out


def write_report(directory: Path, name: str, result: RunResult) -> Path:
    """Write the summary and the equity curve to ``directory``.

    Args:
        directory: Output directory, created if missing.
        name: Base filename.
        result: The run to write.

    Returns:
        The path of the summary file.
    """
    from fxbot.backtest.metrics import write_equity_curve

    directory.mkdir(parents=True, exist_ok=True)
    summary = directory / f"{name}.txt"
    summary.write_text(result.report.summary() + "\n", encoding="utf-8")
    write_equity_curve(directory / f"{name}_equity.csv", result.equity_curve)
    return summary
