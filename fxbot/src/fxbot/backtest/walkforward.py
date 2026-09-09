"""Walk-forward protocol -- the acceptance gate (§11.4).

::

    History: >= 8 years H1
    Fold:    24 months in-sample (optimise)  ->  6 months out-of-sample (never touched)
    Step:    6 months, rolling
    Folds:   >= 8

Optimise on in-sample only. Pick the parameter set by the **centre of the best plateau**
on the ``(adx_min x sl_atr_mult x donchian_period)`` surface -- never the single best cell.
Apply it unchanged to the out-of-sample window. Concatenate all OOS windows: that is the
only equity curve you are allowed to believe.

**If a gate fails, the answer is not to re-optimise.** Re-running the optimiser after
seeing OOS results is a hard ban (§17.16) and this module gives you no way to do it: the
OOS evaluation happens once per fold, inside :func:`run_walkforward`, and the gate report
is produced from the concatenation.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from fxbot.backtest.metrics import BacktestReport
from fxbot.config.schema import AppConfig, StrategyParams
from fxbot.core.models import ClosedTrade

IS_MONTHS = 24
OOS_MONTHS = 6
STEP_MONTHS = 6
MIN_FOLDS = 8


@dataclass(frozen=True, slots=True)
class Fold:
    """One walk-forward fold."""

    index: int
    is_start: datetime
    is_end: datetime
    oos_start: datetime
    oos_end: datetime


@dataclass
class FoldResult:
    """The outcome of one fold: chosen parameters, in-sample and out-of-sample."""

    fold: Fold
    params: StrategyParams
    is_sharpe: float
    oos_report: BacktestReport
    oos_trades: list[ClosedTrade] = field(default_factory=list)
    oos_curve: list[tuple[datetime, float]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GateResult:
    """One go-live criterion and whether the concatenated OOS curve met it."""

    name: str
    threshold: str
    actual: str
    passed: bool


def month_offset(when: datetime, months: int) -> datetime:
    """Return ``when`` shifted by ``months`` calendar months, clamped to month end."""
    total = when.month - 1 + months
    year = when.year + total // 12
    month = total % 12 + 1
    day = min(when.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                         31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return when.replace(year=year, month=month, day=day)


def generate_folds(start: datetime, end: datetime, is_months: int = IS_MONTHS,
                   oos_months: int = OOS_MONTHS,
                   step_months: int = STEP_MONTHS) -> list[Fold]:
    """Return the rolling folds covering ``[start, end]``.

    Args:
        start: First timestamp in the history.
        end: Last timestamp in the history.
        is_months: In-sample length.
        oos_months: Out-of-sample length.
        step_months: Roll step.

    Returns:
        The folds, in order. Possibly fewer than :data:`MIN_FOLDS` -- the caller reports
        that as a failed gate rather than this function quietly shortening the windows.
    """
    folds: list[Fold] = []
    is_start = start
    index = 0
    while True:
        is_end = month_offset(is_start, is_months)
        oos_end = month_offset(is_end, oos_months)
        if oos_end > end:
            break
        folds.append(Fold(index, is_start, is_end, is_end, oos_end))
        index += 1
        is_start = month_offset(is_start, step_months)
    return folds


def parameter_grid(base: StrategyParams,
                   adx_min: Sequence[float] = (15.0, 20.0, 25.0, 30.0),
                   sl_atr_mult: Sequence[float] = (1.5, 2.0, 2.5, 3.0),
                   donchian_period: Sequence[int] = (15, 20, 30, 40)) -> list[StrategyParams]:
    """Return the ``(adx_min x sl_atr_mult x donchian_period)`` surface (§11.4).

    Args:
        base: The parameter set to vary.
        adx_min: ADX floor candidates.
        sl_atr_mult: Stop-distance candidates.
        donchian_period: Channel-length candidates.

    Returns:
        Every combination, as frozen parameter objects.
    """
    out: list[StrategyParams] = []
    for adx, sl, don in itertools.product(adx_min, sl_atr_mult, donchian_period):
        out.append(base.model_copy(update={"adx_min": adx, "sl_atr_mult": sl,
                                           "donchian_period": don}))
    return out


def plateau_centre(scores: Mapping[tuple[float, float, int], float]) -> tuple[float, float, int]:
    """Return the centre of the best plateau on the parameter surface.

    Never the single best cell: the best cell is where the noise happened to be kindest,
    and a system that only works at exactly ``adx_min = 23.5`` does not work.

    Each cell is scored by the **worst** result in its immediate neighbourhood on the grid,
    tie-broken by the neighbourhood mean. That is what "plateau" means operationally: a
    region where even the neighbours are good. Scoring by the neighbourhood *mean* alone
    is not enough -- one spectacular spike drags its own neighbourhood average above a
    genuinely broad, lower plateau, which is precisely the overfit this rule exists to
    reject. ``tests/test_walkforward.py`` builds that exact surface.

    Args:
        scores: ``(adx_min, sl_atr_mult, donchian_period) -> in-sample score``.

    Returns:
        The winning coordinate.

    Raises:
        ValueError: If ``scores`` is empty.
    """
    if not scores:
        raise ValueError("cannot pick a plateau from an empty surface")
    axes = [sorted({key[i] for key in scores}) for i in range(3)]
    index_of = [{value: i for i, value in enumerate(axis)} for axis in axes]

    best_key = next(iter(scores))
    best_rank = (-np.inf, -np.inf)
    for key in scores:
        coords = [index_of[i][key[i]] for i in range(3)]
        neighbourhood = []
        for offsets in itertools.product((-1, 0, 1), repeat=3):
            probe = []
            for axis_i, offset in enumerate(offsets):
                position = coords[axis_i] + offset
                if not 0 <= position < len(axes[axis_i]):
                    break
                probe.append(axes[axis_i][position])
            else:
                value = scores.get(tuple(probe))  # type: ignore[arg-type]
                if value is not None and np.isfinite(value):
                    neighbourhood.append(value)
        if not neighbourhood:
            continue
        rank = (float(np.min(neighbourhood)), float(np.mean(neighbourhood)))
        if rank > best_rank:
            best_rank, best_key = rank, key
    return best_key


def slice_frames(frames: Mapping[str, pd.DataFrame], start: datetime,
                 end: datetime) -> dict[str, pd.DataFrame]:
    """Return each frame restricted to ``[start, end)``."""
    return {symbol: frame[(frame.index >= start) & (frame.index < end)]
            for symbol, frame in frames.items()}


def run_walkforward(
    cfg: AppConfig,
    frames: Mapping[str, pd.DataFrame],
    evaluate: Callable[[AppConfig, Mapping[str, pd.DataFrame]], BacktestReport],
    grid: Sequence[StrategyParams] | None = None,
    warmup_bars: int | None = None,
) -> list[FoldResult]:
    """Run the walk-forward protocol and return one result per fold.

    Args:
        cfg: The resolved configuration; ``cfg.strategy`` is the base parameter set.
        frames: The full history, ``symbol -> ascending bar frame``.
        evaluate: Runs one backtest and returns its report. Injected so this module holds
            no engine dependency and can be tested on a stub.
        grid: The parameter surface; defaults to :func:`parameter_grid`.
        warmup_bars: Bars prepended to each window so indicators are warm. Defaults to
            ``cfg.strategy.warmup_bars``; §11.5 requires the warmup bars be discarded from
            results, which is what prepending rather than shortening achieves.

    Returns:
        One :class:`FoldResult` per fold, in order.
    """
    first = min(frame.index[0].to_pydatetime() for frame in frames.values())
    last = max(frame.index[-1].to_pydatetime() for frame in frames.values())
    folds = generate_folds(first, last)
    candidates = list(grid or parameter_grid(cfg.strategy))
    warmup = cfg.strategy.warmup_bars if warmup_bars is None else warmup_bars

    results: list[FoldResult] = []
    for fold in folds:
        scores: dict[tuple[float, float, int], float] = {}
        for params in candidates:
            windows = _warm_slice(frames, fold.is_start, fold.is_end, warmup)
            report = evaluate(cfg.model_copy(update={"strategy": params}), windows)
            scores[(params.adx_min, params.sl_atr_mult, params.donchian_period)] = (
                report.sharpe if report.trade_count >= 10 else -np.inf
            )
        adx, sl, don = plateau_centre(scores)
        chosen = cfg.strategy.model_copy(
            update={"adx_min": adx, "sl_atr_mult": sl, "donchian_period": don})

        # The OOS window is evaluated exactly once, with the parameters already chosen.
        oos_windows = _warm_slice(frames, fold.oos_start, fold.oos_end, warmup)
        oos_report = evaluate(cfg.model_copy(update={"strategy": chosen}), oos_windows)
        results.append(FoldResult(
            fold=fold, params=chosen, is_sharpe=scores[(adx, sl, don)],
            oos_report=oos_report, oos_curve=list(oos_report.equity_curve),
        ))
    return results


def _warm_slice(frames: Mapping[str, pd.DataFrame], start: datetime, end: datetime,
                warmup: int) -> dict[str, pd.DataFrame]:
    """Return each frame over ``[start, end)`` with ``warmup`` bars prepended."""
    out: dict[str, pd.DataFrame] = {}
    for symbol, frame in frames.items():
        position = int(frame.index.searchsorted(start))
        out[symbol] = frame.iloc[max(0, position - warmup):int(frame.index.searchsorted(end))]
    return out


def concatenate_oos(results: Sequence[FoldResult],
                    starting_equity: float) -> tuple[list[ClosedTrade],
                                                     list[tuple[datetime, float]]]:
    """Stitch every fold's out-of-sample window into one ledger and one curve.

    Each fold's curve is rebased onto the running equity so the concatenation compounds
    the way a live account would, rather than restarting at the initial balance eight
    times and flattering the drawdown.

    Args:
        results: The fold results, in order.
        starting_equity: Opening balance of the first fold.

    Returns:
        ``(trades, equity curve)``.
    """
    trades: list[ClosedTrade] = []
    curve: list[tuple[datetime, float]] = []
    equity = starting_equity
    for result in results:
        trades.extend(result.oos_trades)
        fold_curve = result.oos_curve
        if not fold_curve:
            continue
        base = fold_curve[0][1]
        for when, value in fold_curve:
            curve.append((when, equity * (value / base) if base else equity))
        equity = curve[-1][1]
    return trades, curve


def evaluate_gates(report: BacktestReport, results: Sequence[FoldResult],
                   symbols: Sequence[str]) -> list[GateResult]:
    """Score the concatenated OOS curve against the §11.4 go-live criteria.

    Args:
        report: The report built from the concatenated OOS curve.
        results: The fold results, for the fold-level and IS/OOS criteria.
        symbols: The configured universe, for the "profitable symbols" criterion.

    Returns:
        One :class:`GateResult` per criterion, in the order §11.4 lists them.
    """
    profitable_folds = sum(1 for r in results if r.oos_report.net_profit > 0.0)
    fold_share = profitable_folds / len(results) if results else 0.0
    is_sharpes = [r.is_sharpe for r in results if np.isfinite(r.is_sharpe)]
    oos_sharpes = [r.oos_report.sharpe for r in results]
    ratio = (float(np.mean(oos_sharpes)) / float(np.mean(is_sharpes))
             if is_sharpes and float(np.mean(is_sharpes)) > 0.0 else 0.0)
    profitable_symbols = sum(1 for s in report.per_symbol if s.net_pnl > 0.0)
    two_x = report.cost_sensitivity.get("2x", float("nan"))

    return [
        GateResult("trade count", ">= 200", str(report.trade_count),
                   report.trade_count >= 200),
        GateResult("profit factor", ">= 1.25", f"{report.profit_factor:.2f}",
                   report.profit_factor >= 1.25),
        GateResult("Sharpe", ">= 0.7", f"{report.sharpe:.2f}", report.sharpe >= 0.7),
        GateResult("max drawdown", "<= 15%", f"{report.max_drawdown_pct:.1f}%",
                   report.max_drawdown_pct <= 15.0),
        GateResult("OOS / IS Sharpe", ">= 0.5", f"{ratio:.2f}", ratio >= 0.5),
        GateResult("profitable folds", ">= 60%", f"{100 * fold_share:.0f}%",
                   fold_share >= 0.6),
        GateResult("profitable symbols", f">= 3 of {len(symbols)}",
                   str(profitable_symbols), profitable_symbols >= 3),
        GateResult("net profit at 2x costs", "> 0", f"{two_x:,.2f}",
                   bool(two_x > 0.0) if two_x == two_x else False),
        GateResult("fold count", f">= {MIN_FOLDS}", str(len(results)),
                   len(results) >= MIN_FOLDS),
    ]


def gate_report(gates: Sequence[GateResult]) -> str:
    """Render the gate table, and say plainly whether the system may go live."""
    width = max(len(g.name) for g in gates) if gates else 10
    lines = [f"{'criterion'.ljust(width)}  {'threshold':<16} {'actual':<12} verdict"]
    lines.append("-" * (width + 42))
    for gate in gates:
        verdict = "PASS" if gate.passed else "FAIL"
        lines.append(f"{gate.name.ljust(width)}  {gate.threshold:<16} {gate.actual:<12} {verdict}")
    failed = [g.name for g in gates if not g.passed]
    lines.append("")
    if failed:
        lines.append(f"VERDICT: DO NOT GO LIVE. Failed: {', '.join(failed)}.")
        lines.append("The answer to a failed gate is not to re-optimise (§11.4, §17.16). "
                     "Propose a structural change, or abandon the strategy.")
    else:
        lines.append("VERDICT: all §11.4 gates passed on the concatenated out-of-sample "
                     "curve. Proceed to §13.5 step 2 (paper/replay), not to live.")
    return "\n".join(lines)


def iter_fold_windows(frames: Mapping[str, pd.DataFrame],
                      folds: Sequence[Fold]) -> Iterator[tuple[Fold, dict[str, pd.DataFrame]]]:
    """Yield ``(fold, out-of-sample frames)`` for inspection and testing."""
    for fold in folds:
        yield fold, slice_frames(frames, fold.oos_start, fold.oos_end)
