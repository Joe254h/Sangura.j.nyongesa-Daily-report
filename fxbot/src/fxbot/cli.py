"""The ``fxbot`` command line (§3).

``backtest | walkforward | live | download | kill | reset | flatten | status``.

Every promotion between demo and live is a ``--env`` change, never a code change (§13.5).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from fxbot.config.loader import load_config, load_secrets, redacted
from fxbot.config.schema import AppConfig
from fxbot.core.clock import ServerClock
from fxbot.core.enums import RiskStatus
from fxbot.core.errors import FxBotError
from fxbot.core.models import JournalSink, SymbolSpec
from fxbot.execution.broker import Broker
from fxbot.runtime.engine import TradingEngine

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

    from fxbot.backtest.metrics import BacktestReport
from fxbot.ops.alerts import Alerter
from fxbot.ops.health import Health, utc_now
from fxbot.ops.logging import configure_logging
from fxbot.risk.governor import RiskGovernor
from fxbot.runtime.journal import Journal

app = typer.Typer(
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Trend-following FX bot for MetaTrader 5.",
)

# Typed per parameter rather than shared as one module-level `typer.Option` object: a
# single Option instance reused across commands is stateful in Click and silently stops
# parsing on every command but the first.
EnvOption = Annotated[str, typer.Option(help="demo | live | backtest")]
ResearchEnvOption = Annotated[str, typer.Option(help="normally backtest")]


def _bootstrap(env: str, console: bool = True) -> tuple[AppConfig, Journal]:
    """Load config, configure logging and open the journal. Shared by every command."""
    cfg = load_config(env)
    configure_logging(cfg.paths.log_dir, cfg.env, console=console)
    journal = Journal(cfg.paths.journal_path)
    return cfg, journal


def _alerter(cfg: AppConfig, secrets: Mapping[str, str]) -> Alerter:
    """Build the alerter from config plus environment secrets."""
    return Alerter(cfg.alerts.enabled, cfg.alerts.min_severity,
                   secrets.get("TELEGRAM_BOT_TOKEN"), secrets.get("TELEGRAM_CHAT_ID"),
                   cfg.env)


@app.command()
def status(env: EnvOption = "demo") -> None:
    """Print the risk status, open positions and the recent reject histogram."""
    cfg, journal = _bootstrap(env, console=False)
    clock = ServerClock(0)
    governor = RiskGovernor(cfg, cfg.paths.risk_state_path, clock, journal)
    governor.load()
    state = governor.state
    typer.echo(f"env               {cfg.env}")
    typer.echo(f"risk status       {state.status}")
    typer.echo(f"trading day       {state.trading_day}")
    typer.echo(f"day start equity  {state.day_start_equity:,.2f}")
    typer.echo(f"equity HWM        {state.equity_hwm:,.2f}")
    typer.echo(f"realised today    {state.realised_pnl_today:,.2f}")
    typer.echo(f"consecutive loss  {state.consecutive_losses}")
    typer.echo(f"trades today      {state.trades_today}")
    typer.echo(f"open tickets      {state.open_tickets}")
    if state.status is RiskStatus.HALTED:
        typer.echo(f"halted reason     {state.halted_reason}")
        typer.echo(f"halted at         {state.halted_at}")
    since = (datetime.now() - timedelta(days=7)).isoformat()
    histogram = journal.reject_histogram(since)
    if histogram:
        typer.echo("\nreject reasons (7 days):")
        for reason, count in histogram.items():
            typer.echo(f"  {reason:<20} {count:>8d}")
    journal.close()


@app.command()
def kill(
    env: EnvOption = "demo",
    reason: Annotated[str, typer.Option(help="Recorded verbatim")] = "manual kill switch",
) -> None:
    """Halt the bot immediately. Open positions keep their broker-side stops."""
    cfg, journal = _bootstrap(env, console=False)
    governor = RiskGovernor(cfg, cfg.paths.risk_state_path, ServerClock(0), journal)
    governor.load()
    governor.halt(reason)
    typer.echo(f"HALTED: {reason}")
    typer.echo("Open positions were NOT closed. Use `fxbot flatten` if that is what you want.")
    journal.close()


@app.command()
def reset(
    operator: Annotated[str, typer.Option(help="Who is authorising this. Recorded verbatim.")],
    env: EnvOption = "demo",
) -> None:
    """Clear ``HALTED`` back to ``NORMAL``. The only way out of a halt (§8.5)."""
    cfg, journal = _bootstrap(env, console=False)
    governor = RiskGovernor(cfg, cfg.paths.risk_state_path, ServerClock(0), journal)
    governor.load()
    if governor.status is not RiskStatus.HALTED:
        typer.echo(f"not halted (status is {governor.status}); nothing to reset")
        journal.close()
        raise typer.Exit(code=1)
    previous = governor.state.halted_reason
    governor.manual_reset(operator)
    _alerter(cfg, load_secrets()).critical(
        f"HALT cleared by {operator}. Previous reason: {previous}")
    typer.echo(f"reset to NORMAL by {operator} (was: {previous})")
    journal.close()


@app.command()
def flatten(
    env: EnvOption = "demo",
    confirm: Annotated[bool, typer.Option("--yes",
                                          help="Required: this closes real positions.")] = False,
) -> None:
    """Close every bot position at market."""
    if not confirm:
        typer.echo("refusing to flatten without --yes")
        raise typer.Exit(code=1)
    cfg, journal = _bootstrap(env)
    engine = _live_engine(cfg, journal)
    closed = engine.flatten("manual")
    typer.echo(f"closed {closed} position(s)")
    journal.close()


@app.command()
def download(
    env: EnvOption = "demo",
    years: Annotated[int, typer.Option(help="0 uses cfg.data.history_years")] = 0,
    dump_specs: Annotated[bool, typer.Option("--dump-specs")] = False,
) -> None:
    """Bulk-download H1 history to parquet, and optionally capture symbol specs."""
    from scripts.download_history import download_history  # noqa: PLC0415

    cfg, journal = _bootstrap(env)
    journal.close()
    download_history(cfg, years or cfg.data.history_years, dump_specs)


@app.command()
def live(
    env: EnvOption = "demo",
    max_cycles: Annotated[int, typer.Option(
        help="0 runs until stopped; used by soak tests")] = 0,
) -> None:
    """Run the live loop. This is what the NSSM service starts (§13.4)."""
    cfg, journal = _bootstrap(env)
    from fxbot.runtime.scheduler import Scheduler  # noqa: PLC0415

    engine = _live_engine(cfg, journal)
    scheduler = Scheduler(cfg, engine.clock, engine, engine.governor, engine.alerter, utc_now)
    scheduler.install_signal_handlers()
    typer.echo(json.dumps(redacted(cfg))[:200] + " ...")
    completed = scheduler.run_forever(max_cycles or None)
    typer.echo(f"stopped after {completed} cycle(s)")
    journal.close()


@app.command()
def backtest(
    env: ResearchEnvOption = "backtest",
    symbol: Annotated[str, typer.Option(help="Default: every configured symbol")] = "",
    engine: Annotated[str, typer.Option(help="backtrader | replay")] = "backtrader",
    out: Annotated[Path, typer.Option(help="Where to write the report")] = Path("reports"),
) -> None:
    """Run one backtest over the cached history and write the report."""
    from fxbot.backtest.runner import run_backtrader, write_report  # noqa: PLC0415
    from fxbot.data.cache import read_cache  # noqa: PLC0415
    from fxbot.runtime.engine import replay_history  # noqa: PLC0415

    cfg, journal = _bootstrap(env)
    clock = ServerClock(_offset_from_state(cfg))
    symbols = [symbol] if symbol else list(cfg.symbols)
    frames = {}
    for name in symbols:
        frame = read_cache(cfg.paths.data_dir, name, cfg.timeframe)
        if frame is None:
            typer.echo(f"no cached history for {name}; run `fxbot download` first")
            raise typer.Exit(code=1)
        frames[name] = frame
    specs = _load_specs(symbols)

    def factory(sink: JournalSink = journal) -> RiskGovernor:
        gov = RiskGovernor(cfg, cfg.paths.state_dir / "backtest_risk_state.json", clock, sink)
        gov.load()
        gov.set_symbol_specs(specs)
        return gov

    if engine == "replay":
        result = replay_history(cfg, clock, frames, specs, factory, journal,
                                _alerter(cfg, {}))
    else:
        result = run_backtrader(cfg, clock, frames, specs, factory())
    typer.echo(result.report.summary())
    path = write_report(out, f"backtest_{engine}", result)
    typer.echo(f"\nwritten: {path}")
    journal.close()


@app.command()
def walkforward(
    env: ResearchEnvOption = "backtest",
    out: Annotated[Path, typer.Option(help="Where to write the report")] = Path("reports"),
) -> None:
    """Run the §11.4 walk-forward protocol and print the go-live gate table."""
    from fxbot.backtest.metrics import build_report, write_equity_curve  # noqa: PLC0415
    from fxbot.backtest.runner import run_backtrader  # noqa: PLC0415
    from fxbot.backtest.walkforward import (  # noqa: PLC0415
        concatenate_oos,
        evaluate_gates,
        gate_report,
        run_walkforward,
    )
    from fxbot.data.cache import read_cache  # noqa: PLC0415
    from fxbot.runtime.journal import NullJournal  # noqa: PLC0415

    cfg, journal = _bootstrap(env)
    clock = ServerClock(_offset_from_state(cfg))
    frames = {}
    for name in cfg.symbols:
        frame = read_cache(cfg.paths.data_dir, name, cfg.timeframe)
        if frame is not None:
            frames[name] = frame
    if not frames:
        typer.echo("no cached history; run `fxbot download` first")
        raise typer.Exit(code=1)
    specs = _load_specs(list(frames))

    def evaluate(config: AppConfig, windows: Mapping[str, pd.DataFrame]) -> BacktestReport:
        gov = RiskGovernor(config, cfg.paths.state_dir / "wf_risk_state.json", clock,
                           NullJournal())
        gov.load()
        gov.set_symbol_specs(specs)
        return run_backtrader(config, clock, windows, specs, gov).report

    results = run_walkforward(cfg, frames, evaluate)
    trades, curve = concatenate_oos(results, 10_000.0)
    report = build_report(trades, curve, 10_000.0, cfg.timeframe_minutes)
    gates = evaluate_gates(report, results, list(cfg.symbols))
    typer.echo(report.summary())
    typer.echo("")
    typer.echo(gate_report(gates))
    out.mkdir(parents=True, exist_ok=True)
    (out / "walkforward.txt").write_text(
        report.summary() + "\n\n" + gate_report(gates) + "\n", encoding="utf-8")
    write_equity_curve(out / "walkforward_oos_equity.csv", curve)
    journal.close()


# ---------------------------------------------------------------------- helpers


def _offset_from_state(cfg: AppConfig) -> int:
    """Return the last probed broker offset, or 0 for research runs.

    Backtests do not talk to a terminal, so there is nothing to probe; the offset only has
    to be *consistent* with the timestamps in the parquet cache, which were written in
    server time already.
    """
    path = cfg.paths.state_dir / "server_offset.json"
    if path.is_file():
        try:
            return int(json.loads(path.read_text(encoding="utf-8"))["offset_hours"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0
    return 0


def _load_specs(symbols: list[str]) -> dict[str, SymbolSpec]:
    """Load captured symbol specifications from ``tests/fixtures/specs``.

    §11.1 is explicit: the backtest sizes through the real ``risk/sizing.py`` with a spec
    **captured from the live broker, not invented**. ``scripts/download_history.py
    --dump-specs`` is what captures them.
    """
    root = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "specs"
    specs: dict[str, SymbolSpec] = {}
    for name in symbols:
        path = root / f"{name}.json"
        if not path.is_file():
            typer.echo(f"missing symbol spec fixture: {path}")
            raise typer.Exit(code=1)
        specs[name] = SymbolSpec(**json.loads(path.read_text(encoding="utf-8")))
    return specs


def _live_engine(cfg: AppConfig, journal: Journal) -> TradingEngine:
    """Build a fully wired live engine: source, broker, governor, alerts, health."""
    from fxbot.data.clock_probe import probe_server_offset  # noqa: PLC0415
    from fxbot.data.mt5_source import MT5DataSource  # noqa: PLC0415
    from fxbot.execution.mt5_broker import MT5Broker  # noqa: PLC0415
    from fxbot.execution.paper_broker import PaperBroker  # noqa: PLC0415
    secrets = load_secrets()
    probe_clock = ServerClock(0)
    source = MT5DataSource(cfg, probe_clock)
    source.connect(secrets)
    resolved = source.resolve_symbols(list(cfg.symbols))
    offset = probe_server_offset(source, next(iter(resolved.values())), utc_now)
    clock = ServerClock(offset)
    cfg.paths.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.paths.state_dir / "server_offset.json").write_text(
        json.dumps({"offset_hours": offset}), encoding="utf-8")

    source = MT5DataSource(cfg, clock)
    source.connect(secrets)
    source.resolve_symbols(list(cfg.symbols))
    specs = {name: source.symbol_spec(broker_name) for name, broker_name in resolved.items()}

    governor = RiskGovernor(cfg, cfg.paths.risk_state_path, clock, journal)
    governor.load()
    governor.set_symbol_specs(specs)
    alerter = _alerter(cfg, secrets)
    health = Health(cfg.alerts.heartbeat_url, cfg.timeframe_minutes,
                    cfg.runtime.watchdog_multiples)

    broker: Broker
    if cfg.execution.dry_run:
        broker = PaperBroker(cfg, specs, {name: [] for name in specs})
    else:
        live_broker = MT5Broker(cfg, source, clock)
        governor.set_margin_calculator(live_broker.order_calc_margin)
        broker = live_broker

    return TradingEngine(cfg, clock, source, broker, governor, journal, alerter, health,
                         resolved)


def main() -> None:
    """Entry point that turns a domain error into a clean non-zero exit."""
    try:
        app()
    except FxBotError as exc:
        typer.echo(f"fxbot: {exc}", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
