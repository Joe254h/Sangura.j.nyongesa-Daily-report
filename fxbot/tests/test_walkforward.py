"""Walk-forward protocol tests (§11.4).

The gate table is the point of this module: **if a criterion fails, the answer is not to
re-optimise.** These tests assert that the plateau rule is a plateau rule, that the
out-of-sample window is evaluated exactly once per fold, and that a failing gate produces
a refusal in plain words.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest
from tests.conftest import SERVER_TZ, load_bars

from fxbot.backtest.metrics import build_report
from fxbot.backtest.walkforward import (
    MIN_FOLDS,
    Fold,
    FoldResult,
    concatenate_oos,
    evaluate_gates,
    gate_report,
    generate_folds,
    month_offset,
    parameter_grid,
    plateau_centre,
    run_walkforward,
    slice_frames,
)

START = datetime(2018, 1, 1, tzinfo=SERVER_TZ)
END = datetime(2026, 1, 1, tzinfo=SERVER_TZ)


def test_eight_years_of_history_yields_at_least_eight_folds() -> None:
    """24 months in-sample, 6 out, stepping 6: §11.4's minimum is comfortably met."""
    folds = generate_folds(START, END)
    assert len(folds) >= MIN_FOLDS
    for fold in folds:
        assert fold.is_end == fold.oos_start, "the OOS window starts where the IS one ends"
        assert (fold.oos_end - fold.oos_start).days >= 180
        assert fold.oos_end <= END


def test_folds_roll_forward_by_the_step() -> None:
    """Consecutive folds are six months apart and their OOS windows do not overlap."""
    folds = generate_folds(START, END)
    for earlier, later in zip(folds, folds[1:], strict=False):
        assert later.is_start > earlier.is_start
        assert later.oos_start >= earlier.oos_end


def test_too_little_history_is_reported_not_papered_over() -> None:
    """Short history gives few folds, and the gate is what says so."""
    folds = generate_folds(START, datetime(2020, 7, 1, tzinfo=SERVER_TZ))
    assert len(folds) < MIN_FOLDS
    report = build_report([], [], 10_000.0)
    gates = evaluate_gates(report, [], ["EURUSD"])
    fold_gate = next(g for g in gates if g.name == "fold count")
    assert not fold_gate.passed


def test_month_offset_clamps_to_the_month_end() -> None:
    """31 January plus one month is 29 February in a leap year, not an error."""
    assert month_offset(datetime(2024, 1, 31), 1) == datetime(2024, 2, 29)
    assert month_offset(datetime(2023, 1, 31), 1) == datetime(2023, 2, 28)
    assert month_offset(datetime(2024, 11, 30), 3) == datetime(2025, 2, 28)


def test_the_parameter_grid_covers_the_three_axes() -> None:
    """§11.4 optimises on ``adx_min x sl_atr_mult x donchian_period`` and nothing else."""
    from fxbot.config.schema import StrategyParams

    grid = parameter_grid(StrategyParams())
    assert len(grid) == 4 * 4 * 4
    assert len({(p.adx_min, p.sl_atr_mult, p.donchian_period) for p in grid}) == len(grid)
    assert len({p.atr_period for p in grid}) == 1, "only the three axes vary"


def test_the_plateau_centre_beats_the_single_best_cell() -> None:
    """A lone spike surrounded by rubbish must lose to a broad, lower plateau (§11.4)."""
    scores: dict[tuple[float, float, int], float] = {}
    for adx in (15.0, 20.0, 25.0, 30.0):
        for sl in (1.5, 2.0, 2.5, 3.0):
            for don in (15, 20, 30, 40):
                scores[(adx, sl, don)] = 0.1
    # A 3x3x3 plateau of 1.0 centred on (25.0, 2.5, 30).
    for adx in (20.0, 25.0, 30.0):
        for sl in (2.0, 2.5, 3.0):
            for don in (20, 30, 40):
                scores[(adx, sl, don)] = 1.0
    # A single spectacular cell far away.
    scores[(15.0, 1.5, 15)] = 9.0

    assert max(scores, key=lambda k: scores[k]) == (15.0, 1.5, 15)
    assert plateau_centre(scores) == (25.0, 2.5, 30)


def test_the_plateau_centre_rejects_an_empty_surface() -> None:
    """Failure path: there is no sensible answer, so there is no answer."""
    with pytest.raises(ValueError):
        plateau_centre({})


def test_slicing_respects_the_window_bounds() -> None:
    """A fold sees only its own window."""
    frame = load_bars("parity_eurusd")
    start = frame.index[100].to_pydatetime()
    end = frame.index[400].to_pydatetime()
    sliced = slice_frames({"EURUSD": frame}, start, end)["EURUSD"]
    assert sliced.index[0] >= start
    assert sliced.index[-1] < end


def test_out_of_sample_is_evaluated_exactly_once_per_fold() -> None:
    """§17.16: re-running the optimiser after seeing OOS results is a hard ban.

    The optimiser touches the in-sample window many times and the out-of-sample window
    once. If that ever inverts, this test fails.
    """
    from fxbot.config.schema import AppConfig, StrategyParams

    frame = load_bars("parity_eurusd")
    frames = {"EURUSD": frame}
    cfg = AppConfig(env="backtest")
    grid = [StrategyParams(adx_min=a) for a in (18.0, 20.0, 22.0)]
    calls: list[tuple[datetime, datetime]] = []

    def evaluate(config, windows):  # noqa: ANN001, ANN202
        window = windows["EURUSD"]
        if len(window):
            calls.append((window.index[0].to_pydatetime(),
                          window.index[-1].to_pydatetime()))
        return build_report([], [(frame.index[0].to_pydatetime(), 10_000.0)], 10_000.0)

    results = run_walkforward(cfg, frames, evaluate, grid=grid, warmup_bars=10)
    folds = generate_folds(frame.index[0].to_pydatetime(), frame.index[-1].to_pydatetime())
    assert len(results) == len(folds)
    # Per fold: len(grid) in-sample evaluations plus exactly one out-of-sample evaluation.
    assert len(calls) == len(folds) * (len(grid) + 1)


def test_concatenation_compounds_rather_than_restarting_each_fold() -> None:
    """Restarting at the initial balance every fold flatters the drawdown (§11.4)."""
    base = datetime(2024, 1, 1, tzinfo=SERVER_TZ)
    results = []
    for index in range(3):
        curve = [(base + timedelta(days=index * 30 + d), 10_000.0 * (1.0 + 0.01 * d))
                 for d in range(5)]
        results.append(FoldResult(
            fold=Fold(index, base, base, base, base),
            params=None,  # type: ignore[arg-type]
            is_sharpe=1.0,
            oos_report=build_report([], curve, 10_000.0),
            oos_curve=curve,
        ))
    _, concatenated = concatenate_oos(results, 10_000.0)
    assert concatenated[0][1] == pytest.approx(10_000.0)
    # Three folds of +4% compound to about +12.5%, not to +4%.
    assert concatenated[-1][1] == pytest.approx(10_000.0 * 1.04**3, rel=1e-6)


def test_every_go_live_criterion_is_scored() -> None:
    """All nine of §11.4's rows appear in the table, thresholds included."""
    report = build_report([], [], 10_000.0)
    gates = evaluate_gates(report, [], ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"])
    names = [g.name for g in gates]
    for expected in ("trade count", "profit factor", "Sharpe", "max drawdown",
                     "OOS / IS Sharpe", "profitable folds", "profitable symbols",
                     "net profit at 2x costs", "fold count"):
        assert expected in names


def test_a_failing_gate_refuses_to_go_live_and_says_why() -> None:
    """The report has to say the words, not leave the reader to infer them."""
    report = build_report([], [], 10_000.0)
    text = gate_report(evaluate_gates(report, [], ["EURUSD"]))
    assert "DO NOT GO LIVE" in text
    assert "not to re-optimise" in text
    assert "trade count" in text


def test_a_passing_table_still_does_not_send_anyone_to_live() -> None:
    """All gates green means proceed to §13.5 step 2 -- paper, then demo, then live."""
    from fxbot.backtest.walkforward import GateResult

    gates = [GateResult("trade count", ">= 200", "412", True),
             GateResult("Sharpe", ">= 0.7", "0.94", True)]
    text = gate_report(gates)
    assert "all §11.4 gates passed" in text
    assert "not to live" in text


def test_the_bootstrap_gives_a_drawdown_distribution() -> None:
    """§11.5: if the realised max DD sits at the 5th percentile, the ordering got lucky."""
    from fxbot.backtest.metrics import bootstrap_drawdowns
    from fxbot.core.enums import Side
    from fxbot.core.models import ClosedTrade

    when = datetime(2024, 1, 1, tzinfo=SERVER_TZ)
    trades = [
        ClosedTrade(ticket=i, symbol="EURUSD", side=Side.BUY, volume=0.1,
                    entry_price=1.08, exit_price=1.081, entry_time=when, exit_time=when,
                    initial_stop=1.078, gross_pnl=0.0, commission=0.0, swap=0.0,
                    net_pnl=(-120.0 if i % 3 else 200.0), r_multiple=0.0, mae_r=0.0,
                    mfe_r=0.0, exit_reason="stop", magic=1)
        for i in range(60)
    ]
    distribution = bootstrap_drawdowns(trades, 10_000.0, iterations=200)
    assert distribution.size == 200
    assert np.all(distribution >= 0.0)
    assert float(np.percentile(distribution, 95)) >= float(np.percentile(distribution, 5))


def test_an_empty_ledger_bootstraps_to_nothing() -> None:
    """Failure path: no trades, no distribution, no exception."""
    from fxbot.backtest.metrics import bootstrap_drawdowns

    assert bootstrap_drawdowns([], 10_000.0).size == 0
