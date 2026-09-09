"""Backtest reporting (§11.3).

Every number here is computed from the :class:`~fxbot.core.models.ClosedTrade` ledger and
the equity curve, both of which already carry commission, spread, slippage and swap.
Reporting results without all four is a hard ban (§17.17), so there is deliberately no
"gross" variant of any of these figures.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import numpy.typing as npt

from fxbot.backtest.costs import FillModel
from fxbot.config.schema import AppConfig
from fxbot.core.errors import ConfigError
from fxbot.core.models import ClosedTrade, SymbolSpec

_SWAP_MODE_POINTS = 0
"""``SYMBOL_SWAP_MODE_POINTS`` -- the only swap mode the cost model can convert."""

TRADING_DAYS_PER_YEAR = 252
"""FX trades five days a week; 252 is the conventional annualisation base."""


@dataclass(frozen=True, slots=True)
class SymbolStats:
    """Per-symbol slice of the report."""

    symbol: str
    trades: int
    net_pnl: float
    win_rate: float
    expectancy_r: float
    profit_factor: float


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """The full result of one backtest or one concatenated out-of-sample curve."""

    net_profit: float
    starting_equity: float
    ending_equity: float
    cagr: float
    max_drawdown_pct: float
    max_drawdown_duration_days: float
    sharpe: float
    sortino: float
    mar: float
    profit_factor: float
    expectancy_r: float
    avg_win_r: float
    avg_loss_r: float
    win_rate: float
    trade_count: int
    max_consecutive_losses: int
    avg_holding_bars: float
    exposure_pct: float
    per_symbol: tuple[SymbolStats, ...] = ()
    per_regime: Mapping[str, int] = field(default_factory=dict)
    reject_histogram: Mapping[str, int] = field(default_factory=dict)
    cost_sensitivity: Mapping[str, float] = field(default_factory=dict)
    equity_curve: tuple[tuple[datetime, float], ...] = ()

    def summary(self) -> str:
        """Return a human-readable block for the console and the runbook."""
        lines = [
            f"net profit         {self.net_profit:>14,.2f}",
            f"CAGR               {100 * self.cagr:>13.2f}%",
            f"max drawdown       {self.max_drawdown_pct:>13.2f}%",
            f"max DD duration    {self.max_drawdown_duration_days:>13.1f} days",
            f"Sharpe             {self.sharpe:>14.2f}",
            f"Sortino            {self.sortino:>14.2f}",
            f"MAR                {self.mar:>14.2f}",
            f"profit factor      {self.profit_factor:>14.2f}",
            f"expectancy         {self.expectancy_r:>14.2f} R",
            f"avg win / loss     {self.avg_win_r:>7.2f}R / {self.avg_loss_r:.2f}R",
            f"win rate           {100 * self.win_rate:>13.1f}%",
            f"trades             {self.trade_count:>14d}",
            f"max consec losses  {self.max_consecutive_losses:>14d}",
            f"avg holding        {self.avg_holding_bars:>14.1f} bars",
            f"exposure           {self.exposure_pct:>13.1f}%",
        ]
        if self.per_symbol:
            lines.append("")
            lines.append("per symbol:")
            for stats in self.per_symbol:
                lines.append(
                    f"  {stats.symbol:<10} {stats.trades:>4d} trades  "
                    f"{stats.net_pnl:>12,.2f}  PF {stats.profit_factor:>5.2f}  "
                    f"exp {stats.expectancy_r:>5.2f}R  win {100 * stats.win_rate:>5.1f}%")
        if self.cost_sensitivity:
            lines.append("")
            lines.append("cost sensitivity (net profit):")
            for label, value in self.cost_sensitivity.items():
                lines.append(f"  {label:<8} {value:>14,.2f}")
        if self.reject_histogram:
            lines.append("")
            lines.append("reject reasons:")
            for reason, count in sorted(self.reject_histogram.items(),
                                        key=lambda kv: -kv[1]):
                lines.append(f"  {reason:<20} {count:>8d}")
        return "\n".join(lines)


def _drawdown(curve: Sequence[tuple[datetime, float]]) -> tuple[float, float]:
    """Return ``(max drawdown %, longest drawdown in days)``."""
    if len(curve) < 2:
        return 0.0, 0.0
    peak = curve[0][1]
    peak_at = curve[0][0]
    worst = 0.0
    longest = 0.0
    for when, value in curve:
        if value > peak:
            peak, peak_at = value, when
            continue
        if peak > 0.0:
            worst = max(worst, 100.0 * (peak - value) / peak)
        longest = max(longest, (when - peak_at).total_seconds() / 86400.0)
    return worst, longest


def _daily_returns(curve: Sequence[tuple[datetime, float]]) -> npt.NDArray[np.float64]:
    """Return the series of daily fractional equity returns."""
    if len(curve) < 2:
        return np.zeros(0, dtype=np.float64)
    by_day: dict[datetime, float] = {}
    for when, value in curve:
        by_day[when.date()] = value  # type: ignore[index]
    values = np.array([v for _, v in sorted(by_day.items())], dtype=np.float64)
    if values.size < 2:
        return np.zeros(0)
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(values) / values[:-1]
    finite: npt.NDArray[np.float64] = returns[np.isfinite(returns)]
    return finite


def build_report(
    trades: Sequence[ClosedTrade],
    equity_curve: Sequence[tuple[datetime, float]],
    starting_equity: float,
    timeframe_minutes: int = 60,
    reject_histogram: Mapping[str, int] | None = None,
    entry_regimes: Mapping[tuple[str, datetime], str] | None = None,
    cost_sensitivity: Mapping[str, float] | None = None,
) -> BacktestReport:
    """Compute the full report from a trade ledger and an equity curve.

    Args:
        trades: Completed round trips, in close order.
        equity_curve: ``(server time, equity)`` sampled once per bar.
        starting_equity: Opening balance.
        timeframe_minutes: Bar length, for the holding-period figure.
        reject_histogram: ``reason -> count`` from the journal or the run state.
        entry_regimes: ``(symbol, entry time) -> regime`` for the per-regime breakdown.
        cost_sensitivity: ``label -> net profit`` at 1x, 1.5x and 2x costs.

    Returns:
        The :class:`BacktestReport`.
    """
    curve = list(equity_curve)
    ending = curve[-1][1] if curve else starting_equity
    net = ending - starting_equity

    r_values = np.array([t.r_multiple for t in trades], dtype=np.float64)
    wins = np.array([t.net_pnl for t in trades if t.net_pnl > 0.0], dtype=np.float64)
    losses = np.array([t.net_pnl for t in trades if t.net_pnl < 0.0], dtype=np.float64)
    win_r = np.array([t.r_multiple for t in trades if t.net_pnl > 0.0], dtype=np.float64)
    loss_r = np.array([t.r_multiple for t in trades if t.net_pnl < 0.0], dtype=np.float64)

    gross_win = float(wins.sum()) if wins.size else 0.0
    gross_loss = float(-losses.sum()) if losses.size else 0.0
    profit_factor = gross_win / gross_loss if gross_loss > 0.0 else math.inf if gross_win else 0.0

    max_dd, dd_days = _drawdown(curve)
    returns = _daily_returns(curve)
    sharpe = _annualised(returns)
    downside = returns[returns < 0.0]
    sortino = (float(np.mean(returns)) * TRADING_DAYS_PER_YEAR /
               (float(np.std(downside, ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR))
               if downside.size > 1 and float(np.std(downside, ddof=1)) > 0.0 else 0.0)

    years = _years(curve)
    cagr = ((ending / starting_equity) ** (1.0 / years) - 1.0
            if years > 0.0 and starting_equity > 0.0 and ending > 0.0 else 0.0)
    mar = cagr / (max_dd / 100.0) if max_dd > 0.0 else 0.0

    holding = [
        (t.exit_time - t.entry_time).total_seconds() / (60.0 * timeframe_minutes)
        for t in trades
    ]
    # Fraction of the available symbol-bars that were spent in a position. Dividing by
    # bars alone would report 200% for a two-symbol run that was always fully invested.
    symbols = max(len({t.symbol for t in trades}), 1)
    exposure = (100.0 * sum(holding) / (len(curve) * symbols)) if curve else 0.0

    return BacktestReport(
        net_profit=net,
        starting_equity=starting_equity,
        ending_equity=ending,
        cagr=cagr,
        max_drawdown_pct=max_dd,
        max_drawdown_duration_days=dd_days,
        sharpe=sharpe,
        sortino=sortino,
        mar=mar,
        profit_factor=profit_factor,
        expectancy_r=float(r_values.mean()) if r_values.size else 0.0,
        avg_win_r=float(win_r.mean()) if win_r.size else 0.0,
        avg_loss_r=float(loss_r.mean()) if loss_r.size else 0.0,
        win_rate=(wins.size / len(trades)) if trades else 0.0,
        trade_count=len(trades),
        max_consecutive_losses=max_consecutive_losses(trades),
        avg_holding_bars=float(np.mean(holding)) if holding else 0.0,
        exposure_pct=min(exposure, 100.0),
        per_symbol=tuple(_per_symbol(trades)),
        per_regime=dict(_per_regime(trades, entry_regimes or {})),
        reject_histogram=dict(reject_histogram or {}),
        cost_sensitivity=dict(cost_sensitivity or {}),
        equity_curve=tuple(curve),
    )


def _annualised(returns: npt.NDArray[np.float64]) -> float:
    """Return the annualised Sharpe of a daily return series (zero risk-free rate)."""
    if returns.size < 2:
        return 0.0
    sigma = float(np.std(returns, ddof=1))
    if sigma <= 0.0:
        return 0.0
    return float(np.mean(returns)) / sigma * math.sqrt(TRADING_DAYS_PER_YEAR)


def _years(curve: Sequence[tuple[datetime, float]]) -> float:
    """Return the length of the curve in years."""
    if len(curve) < 2:
        return 0.0
    return (curve[-1][0] - curve[0][0]).total_seconds() / (365.25 * 86400.0)


def max_consecutive_losses(trades: Sequence[ClosedTrade]) -> int:
    """Return the longest run of losing trades."""
    worst = run = 0
    for trade in trades:
        run = run + 1 if trade.net_pnl < 0.0 else 0
        worst = max(worst, run)
    return worst


def _per_symbol(trades: Sequence[ClosedTrade]) -> list[SymbolStats]:
    """Return the per-symbol breakdown, ordered by symbol."""
    out: list[SymbolStats] = []
    for symbol in sorted({t.symbol for t in trades}):
        subset = [t for t in trades if t.symbol == symbol]
        wins = [t.net_pnl for t in subset if t.net_pnl > 0.0]
        losses = [-t.net_pnl for t in subset if t.net_pnl < 0.0]
        gross_win, gross_loss = sum(wins), sum(losses)
        out.append(SymbolStats(
            symbol=symbol,
            trades=len(subset),
            net_pnl=sum(t.net_pnl for t in subset),
            win_rate=len(wins) / len(subset) if subset else 0.0,
            expectancy_r=float(np.mean([t.r_multiple for t in subset])) if subset else 0.0,
            profit_factor=(gross_win / gross_loss if gross_loss > 0.0
                           else math.inf if gross_win else 0.0),
        ))
    return out


def _per_regime(trades: Sequence[ClosedTrade],
                entry_regimes: Mapping[tuple[str, datetime], str]) -> Counter[str]:
    """Return the trade count per entry regime."""
    counts: Counter[str] = Counter()
    for trade in trades:
        counts[entry_regimes.get((trade.symbol, trade.entry_time), "UNKNOWN")] += 1
    return counts


def write_equity_curve(path: Path, curve: Sequence[tuple[datetime, float]]) -> Path:
    """Write the equity curve as CSV -- §11.3 asks for the numbers, not just a plot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["time,equity"]
    lines += [f"{when.isoformat()},{value:.6f}" for when, value in curve]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def bootstrap_drawdowns(trades: Sequence[ClosedTrade], starting_equity: float,
                        iterations: int = 1000,
                        seed: int = 12345) -> npt.NDArray[np.float64]:
    """Return the drawdown distribution over ``iterations`` shuffles of the trade order.

    §11.5's randomised trade-order test: if the realised max drawdown sits at the 5th
    percentile of this distribution, the backtest got lucky on ordering, not on edge.

    Args:
        trades: The trade ledger.
        starting_equity: Opening balance.
        iterations: How many shuffles.
        seed: Fixed so the answer is reproducible.

    Returns:
        An array of max-drawdown percentages, one per shuffle.
    """
    pnl = np.array([t.net_pnl for t in trades], dtype=np.float64)
    if pnl.size == 0:
        return np.zeros(0)
    rng = np.random.default_rng(seed)
    out = np.empty(iterations, dtype=np.float64)
    for i in range(iterations):
        curve = starting_equity + np.cumsum(rng.permutation(pnl))
        running_peak = np.maximum.accumulate(np.concatenate([[starting_equity], curve]))
        values = np.concatenate([[starting_equity], curve])
        with np.errstate(divide="ignore", invalid="ignore"):
            drawdowns = 100.0 * (running_peak - values) / running_peak
        out[i] = float(np.nanmax(drawdowns))
    return out


@dataclass
class RunResult:
    """A completed run: the ledger, the curve and the report."""

    trades: list[ClosedTrade]
    equity_curve: list[tuple[datetime, float]]
    report: BacktestReport
    reject_histogram: dict[str, int]


def build_models(cfg: AppConfig, specs: Mapping[str, SymbolSpec],
                 spread_multiplier: float = 1.0,
                 slippage_multiplier: float = 1.0) -> dict[str, FillModel]:
    """Build one :class:`~fxbot.backtest.costs.FillModel` per symbol."""
    return {
        symbol: FillModel(
            spec=spec,
            commission_per_lot_per_side=cfg.costs.commission_per_lot_per_side,
            slippage_points=cfg.costs.slippage(symbol),
            spread_source=cfg.costs.spread_source,
            fixed_spread_points=cfg.costs.fixed_spread(symbol),
            spread_multiplier=spread_multiplier,
            slippage_multiplier=slippage_multiplier,
        )
        for symbol, spec in specs.items()
    }


def narrow_to(cfg: AppConfig, symbols: Sequence[str]) -> AppConfig:
    """Return ``cfg`` restricted to ``symbols``, with the clusters filtered to match.

    Both runners call this, so a run over two symbols sees exactly the same effective
    configuration in either engine. Doing it in only one of them would give the two
    governors different correlated-cluster memberships and break parity on a trade neither
    engine actually disagreed about.

    Args:
        cfg: The full configuration.
        symbols: The symbols actually present in the run.

    Returns:
        A copy narrowed to those symbols.
    """
    wanted = list(symbols)
    if list(cfg.symbols) == wanted:
        return cfg
    clusters = {name: [s for s in members if s in wanted]
                for name, members in cfg.risk.clusters.items()}
    risk = cfg.risk.model_copy(update={"clusters": {k: v for k, v in clusters.items() if v}})
    return cfg.model_copy(update={"symbols": wanted, "risk": risk})


def assert_swaps_modelled(specs: Mapping[str, SymbolSpec]) -> None:
    """Refuse to report a backtest whose swap costs cannot be modelled.

    §17.17 bans reporting results without commission, spread, slippage **and swap**. The
    cost model only understands ``SYMBOL_SWAP_MODE_POINTS``; a symbol quoting swaps any
    other way would silently contribute zero financing cost, so the run stops instead.

    Args:
        specs: The symbol specifications in the run.

    Raises:
        ConfigError: Naming every symbol whose swap mode is unmodelled.
    """
    unmodelled = [
        spec.name for spec in specs.values()
        if spec.swap_mode != _SWAP_MODE_POINTS and (spec.swap_long or spec.swap_short)
    ]
    if unmodelled:
        raise ConfigError(
            f"swap_mode is not SYMBOL_SWAP_MODE_POINTS for {unmodelled}; the cost model "
            "cannot convert those swaps, and reporting a backtest without swap costs is "
            "banned (§17.17). Capture the correct specs or extend costs.FillModel.swap."
        )
