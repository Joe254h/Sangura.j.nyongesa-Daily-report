"""The trading engine: the canonical order of operations (§10.2).

Managing before opening matters: a full position book must still get its trailing stops
even when new entries are blocked. Reconciliation runs before anything else, and no order
is ever sent before it succeeds -- doubling a position because a restart lost state is a
top-three failure mode for retail bots (§8.6).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol, cast

import pandas as pd
import structlog

# `backtest.metrics` and `backtest.replay` are backtrader-free by construction, which
# `tests/test_layering.py` asserts transitively: the live engine must never pull a frozen
# research dependency into the process that sends orders (§17.15).
from fxbot.backtest.metrics import (
    RunResult,
    assert_swaps_modelled,
    build_report,
    narrow_to,
)
from fxbot.backtest.replay import ReplayDataSource, frame_to_bars
from fxbot.config.schema import AppConfig
from fxbot.core.clock import ServerClock
from fxbot.core.enums import Bias, IntentKind, Regime, RejectReason, RiskStatus, Side
from fxbot.core.errors import DataUnavailableError, ReconciliationError
from fxbot.core.models import (
    AccountState,
    ClosedTrade,
    Intent,
    Position,
    QualityReport,
    Signal,
    StrategyContext,
    SymbolSpec,
)
from fxbot.data import quality
from fxbot.data.resample import resample_h1_to_d1
from fxbot.execution.broker import Broker
from fxbot.execution.paper_broker import PaperBroker
from fxbot.ops.alerts import Alerter
from fxbot.ops.health import Health
from fxbot.risk.governor import RiskGovernor
from fxbot.runtime.journal import Journal
from fxbot.strategy.manage import manage_position
from fxbot.strategy.trend_donchian import generate_signal

log = structlog.get_logger(__name__)

MAX_RECONCILIATION_FAILURES = 3
"""Three consecutive failures halt the bot (§8.6 step 6)."""

_TIMEFRAME_H1 = 16385
"""``mt5.TIMEFRAME_H1``; the engine never imports MetaTrader5 to learn a constant."""


class DataSource(Protocol):
    """What the engine needs from a market-data adapter.

    Satisfied by :class:`~fxbot.data.mt5_source.MT5DataSource` in live and by
    :class:`~fxbot.backtest.replay.ReplayDataSource` in the parity test -- which is the
    whole point: the engine cannot tell them apart.
    """

    def bars(self, symbol: str, timeframe: int, count: int) -> pd.DataFrame: ...
    def account(self) -> AccountState: ...
    def tick(self, symbol: str) -> tuple[float, float, datetime]: ...
    def symbol_spec(self, symbol: str) -> SymbolSpec: ...


@dataclass
class _PositionMeta:
    """What the broker does not store about a position (§4)."""

    initial_stop: float
    initial_volume: float
    partial_taken: bool = False


class TradingEngine:
    """Owns the loop. The only place that orchestrates."""

    def __init__(
        self,
        cfg: AppConfig,
        clock: ServerClock,
        source: DataSource,
        broker: Broker,
        governor: RiskGovernor,
        journal: Journal,
        alerter: Alerter,
        health: Health,
        symbol_map: Mapping[str, str] | None = None,
    ) -> None:
        """Wire the engine.

        Args:
            cfg: The resolved configuration.
            clock: The broker clock.
            source: The market-data adapter.
            broker: The execution adapter.
            governor: The risk governor, already :meth:`~RiskGovernor.load`-ed.
            journal: The journal.
            alerter: The alerter.
            health: Heartbeat and watchdog.
            symbol_map: ``canonical -> broker symbol``. Identity when omitted.
        """
        self.cfg = cfg
        self.clock = clock
        self.source = source
        self.broker = broker
        self.governor = governor
        self.journal = journal
        self.alerter = alerter
        self.health = health
        self.symbol_map = dict(symbol_map or {s: s for s in cfg.symbols})
        self._meta: dict[int, _PositionMeta] = {}
        self._reconcile_failures = 0
        self._last_cycle_time: datetime | None = None
        self.cycles = 0

    # ------------------------------------------------------------------ context

    def build_context(
        self,
        symbol: str,
        position: Position | None = None,
        strict: bool = True,
    ) -> StrategyContext | None:
        """Assemble the strategy context for one symbol.

        Args:
            symbol: Canonical symbol name.
            position: The open position, if any.
            strict: When True (entries), a quality failure returns None and the caller
                skips the symbol. When False (management, §10.2 step 7), the failure is
                **recorded on the context** rather than raised, so an open trade is still
                managed during a data outage -- a data outage must not orphan a position.

        Returns:
            The context, or None when ``strict`` and the data is unusable.
        """
        broker_symbol = self.symbol_map.get(symbol, symbol)
        spec = self.source.symbol_spec(broker_symbol)
        params = self.cfg.strategy

        try:
            h1 = self.source.bars(broker_symbol, _TIMEFRAME_H1, params.context_bars + 5)
        except DataUnavailableError as exc:
            if strict:
                return None
            h1 = pd.DataFrame()
            report = QualityReport(ok=False, reason=RejectReason.STALE_DATA, detail=str(exc),
                                   bars=0, last_close=datetime.min, gap_count=0,
                                   fatal_sanity=False)
            return self._context(symbol, h1, h1, spec, position, report)

        report = quality.check(h1, symbol, self.cfg.timeframe_minutes, self.clock,
                               self.cfg.data, params)
        if strict and not report.ok:
            log.warning("quality_failed", symbol=symbol, detail=report.detail)
            return None

        now = self._bar_close(h1)
        d1 = resample_h1_to_d1(h1, self.clock, now)
        return self._context(symbol, h1, d1, spec, position, report, now)

    def _context(self, symbol: str, h1: pd.DataFrame, d1: pd.DataFrame, spec: SymbolSpec,
                 position: Position | None, report: QualityReport,
                 now: datetime | None = None) -> StrategyContext:
        """Build the frozen context object."""
        broker_symbol = self.symbol_map.get(symbol, symbol)
        spread_points = self._spread_points(broker_symbol, spec, h1)
        return StrategyContext(
            symbol=symbol,
            now=now or (self._bar_close(h1) if len(h1) else datetime.min),
            h1=h1,
            d1=d1,
            spec=spec,
            current_spread_points=spread_points,
            open_position=position,
            params=self.cfg.strategy,
            session=self.cfg.session,
            max_spread_points=self.cfg.execution.spread_cap(symbol),
            commission_per_lot_round_turn=self.cfg.costs.commission_per_lot_round_turn,
            quality=report,
        )

    def _bar_close(self, h1: pd.DataFrame) -> datetime:
        """Return the close time of the last closed bar."""
        if len(h1) == 0:
            return datetime.min
        last = cast(pd.Timestamp, h1.index[-1]).to_pydatetime()
        return last + timedelta(minutes=self.cfg.timeframe_minutes)

    def _spread_points(self, broker_symbol: str, spec: SymbolSpec,
                       h1: pd.DataFrame) -> int:
        """Return the current spread in points, from the tick, falling back to the bar."""
        try:
            bid, ask, _ = self.source.tick(broker_symbol)
        except DataUnavailableError:
            if len(h1) and "spread" in h1.columns:
                return int(h1["spread"].to_numpy()[-1])
            return self.cfg.execution.spread_cap(broker_symbol) + 1
        if spec.point <= 0.0:
            return 0
        return int(round((ask - bid) / spec.point))

    # ------------------------------------------------------------------ cycle

    def run_cycle(self) -> RiskStatus:
        """Run one full decision cycle. The canonical order of §10.2.

        Returns:
            The risk status after the cycle.

        Raises:
            FatalError: On any condition that must stop the bot; the caller halts the
                governor and alerts (§10.1).
        """
        started = time.perf_counter()
        cycle_id = uuid.uuid4().hex[:12]
        self.journal.set_cycle(cycle_id)

        # 1-2. Connection and account.
        ensure = getattr(self.broker, "ensure_connected", None)
        if callable(ensure):
            ensure()
        account = self.source.account()
        self.clock.observe(account.server_time)

        # 3. Broker day rollover.
        if self._last_cycle_time is not None and self.clock.is_new_trading_day(
                self._last_cycle_time, account.server_time):
            self.governor.on_new_day(account)
        self._last_cycle_time = account.server_time

        # 4-5. Positions and reconciliation. No order is sent before this succeeds.
        positions = self._enrich(self.broker.positions(self.cfg.execution.magic))
        self.reconcile(positions)

        # 6. Status.
        status = self.governor.refresh(account, positions)

        # 7. MANAGE BEFORE OPENING -- always.
        if status is not RiskStatus.HALTED:
            for position in list(positions):
                self._manage(position)
            positions = self._enrich(self.broker.positions(self.cfg.execution.magic))

        # 8. Entries blocked?
        if status in (RiskStatus.DAILY_LOCKOUT, RiskStatus.HALTED):
            self.journal.record_cycle(cycle_id, time.perf_counter() - started, 0, status)
            self.health.heartbeat(account.server_time)
            self.cycles += 1
            return status

        # 9. Entries, one symbol at a time.
        held = {p.symbol for p in positions}
        considered = 0
        for symbol in self.cfg.symbols:
            if symbol in held:
                continue
            considered += 1
            positions = self._consider_entry(symbol, account, positions)

        # 10. Heartbeat.
        self.journal.record_cycle(cycle_id, time.perf_counter() - started, considered, status)
        self.health.heartbeat(account.server_time)
        self.cycles += 1
        return status

    def _consider_entry(self, symbol: str, account: AccountState,
                        positions: list[Position]) -> list[Position]:
        """Evaluate one symbol for a new entry and act on the verdict."""
        ctx = self.build_context(symbol)
        if ctx is None:
            return positions

        signal = generate_signal(ctx)
        # EVERY decision is recorded, including rejections (§10.3).
        self.journal.record_decision(signal, {"symbol": symbol, "now": ctx.now.isoformat()})
        if signal.side is None:
            return positions

        approval = self.governor.approve(signal, ctx, account, positions)
        self.journal.record_approval(approval)
        if not approval.ok or approval.order is None:
            return positions

        result = self.broker.open(approval.order)
        self.governor.record_fill(approval.order, result)
        self.journal.record_order(approval.order, result,
                                  getattr(self.broker, "last_request", None))
        if result.ok and result.ticket is not None:
            self._meta[result.ticket] = _PositionMeta(
                initial_stop=approval.order.stop_price,
                initial_volume=approval.order.volume,
            )
            self.alerter.info(
                f"{symbol} {signal.side} {approval.order.volume} @ {result.filled_price} "
                f"stop {approval.order.stop_price} risk {approval.order.risk_pct:.2f}%")
        else:
            self.alerter.warning(f"{symbol}: order rejected -- {result.comment}")
        # Refresh so the exposure caps see the new position (§10.2 step 9).
        return self._enrich(self.broker.positions(self.cfg.execution.magic))

    def _manage(self, position: Position) -> None:
        """Run the exit logic for one open position and execute its single intent."""
        ctx = self.build_context(position.symbol, position=position, strict=False)
        if ctx is None:
            return
        if ctx.quality.fatal_sanity:
            # Bad ticks only: suppress even MODIFY_STOP; the broker-side stop remains the
            # backstop (§6.4, §10.2 step 7).
            self.journal.record_decision(
                Signal(side=None, regime=Regime.RANGING, bias=Bias.NEUTRAL, entry_ref=0.0,
                       stop_price=0.0, atr=float("nan"), adx=float("nan"),
                       reason=RejectReason.STALE_DATA,
                       diagnostics={"fatal_sanity": 1.0}),
                {"symbol": position.symbol, "now": ctx.now.isoformat()},
            )
            return
        self.execute(manage_position(ctx), position)

    def execute(self, intent: Intent, position: Position) -> None:
        """Carry out one intent. Never batches two (§7.4)."""
        if intent.kind is IntentKind.NONE:
            return
        ticket = intent.ticket or position.ticket

        if intent.kind is IntentKind.CLOSE:
            result = self.broker.close(ticket, None, intent.reason)
            if result.ok:
                self._settle(ticket)
                self.alerter.info(f"{position.symbol}: closed ({intent.reason})")
            return

        if intent.kind is IntentKind.CLOSE_PARTIAL:
            fraction = intent.close_fraction or 0.0
            spec = self.source.symbol_spec(self.symbol_map.get(position.symbol,
                                                               position.symbol))
            volume = spec.floor_volume(position.volume * fraction)
            result = self.broker.close(ticket, volume, intent.reason)
            if result.ok:
                meta = self._meta.get(ticket)
                if meta is not None:
                    meta.partial_taken = True
                self.alerter.info(
                    f"{position.symbol}: banked {volume} at {intent.reason}")
            return

        if intent.kind is IntentKind.MODIFY_STOP and intent.stop_price is not None:
            result = self.broker.modify_stop(ticket, intent.stop_price, None)
            if not result.ok:
                self.alerter.warning(
                    f"{position.symbol}: stop modify rejected -- {result.comment}")

    # ------------------------------------------------------------------ reconciliation

    def _enrich(self, positions: Sequence[Position]) -> list[Position]:
        """Attach the bot-managed fields the broker does not store."""
        out: list[Position] = []
        for position in positions:
            meta = self._meta.get(position.ticket)
            if meta is None:
                meta = _PositionMeta(
                    initial_stop=position.initial_stop or position.stop_loss,
                    initial_volume=position.initial_volume or position.volume,
                    partial_taken=position.partial_taken,
                )
                self._meta[position.ticket] = meta
            out.append(Position(
                ticket=position.ticket, symbol=position.symbol, side=position.side,
                volume=position.volume, entry_price=position.entry_price,
                stop_loss=position.stop_loss, take_profit=position.take_profit,
                open_time=position.open_time, profit=position.profit, magic=position.magic,
                comment=position.comment, initial_stop=meta.initial_stop,
                initial_volume=meta.initial_volume, partial_taken=meta.partial_taken,
            ))
        return out

    def reconcile(self, positions: Sequence[Position]) -> None:
        """Make the bot's view of open positions equal the broker's (§8.6).

        Args:
            positions: What the broker reports right now.

        Raises:
            ReconciliationError: After three consecutive failures. Never send an order
                before reconciliation succeeds.
        """
        try:
            broker_tickets = {p.ticket for p in positions}
            known = set(self.governor.state.open_tickets)

            for position in positions:
                if position.ticket not in known:
                    stops = self.journal.open_position_stops()
                    initial = stops.get(position.ticket, position.stop_loss)
                    self._meta.setdefault(position.ticket, _PositionMeta(
                        initial_stop=initial, initial_volume=position.volume))
                    orphan = position.ticket not in stops
                    log.warning("adopted_position", ticket=position.ticket,
                                symbol=position.symbol, orphan=orphan,
                                initial_stop=initial)
                    if orphan:
                        self.alerter.warning(
                            f"adopted orphan position {position.ticket} on {position.symbol}; "
                            f"initial stop assumed to be the current stop {initial}")

            vanished = known - broker_tickets
            if vanished:
                self._settle_vanished(vanished)

            self.governor.state.open_tickets = sorted(broker_tickets)
            self.governor.save()
            self._reconcile_failures = 0
        except ReconciliationError:
            raise
        except (OSError, ValueError, KeyError) as exc:
            self._reconcile_failures += 1
            log.error("reconciliation_failed", attempt=self._reconcile_failures, error=str(exc))
            self.alerter.critical(f"reconciliation failed ({self._reconcile_failures}): {exc}")
            if self._reconcile_failures >= MAX_RECONCILIATION_FAILURES:
                raise ReconciliationError(
                    f"{self._reconcile_failures} consecutive reconciliation failures") from exc

    def _settle_vanished(self, tickets: set[int]) -> None:
        """Record trades that closed while the bot was down (§8.6 step 4)."""
        since = (self._last_cycle_time or self.clock.now()) - timedelta(days=7)
        recorded = self.journal.recorded_tickets()
        for trade in self.broker.closed_deals(since, self.cfg.execution.magic):
            if trade.ticket in tickets and trade.ticket not in recorded:
                meta = self._meta.get(trade.ticket)
                if meta is not None:
                    trade = _with_initial_stop(trade, meta.initial_stop)
                self.governor.record_closed_trade(trade)
                self.alerter.info(
                    f"{trade.symbol}: reconciled close {trade.net_pnl:+.2f} "
                    f"({trade.exit_reason})")
                self._meta.pop(trade.ticket, None)

    def _settle(self, ticket: int) -> None:
        """Record a trade the bot itself just closed."""
        since = (self._last_cycle_time or self.clock.now()) - timedelta(hours=1)
        recorded = self.journal.recorded_tickets()
        for trade in self.broker.closed_deals(since, self.cfg.execution.magic):
            if trade.ticket == ticket and trade.ticket not in recorded:
                meta = self._meta.get(ticket)
                if meta is not None:
                    trade = _with_initial_stop(trade, meta.initial_stop)
                self.governor.record_closed_trade(trade)
                break
        self._meta.pop(ticket, None)

    def flatten(self, reason: str = "manual") -> int:
        """Close every bot position at market. Used by ``fxbot flatten`` and the runbook.

        Args:
            reason: Recorded as the exit reason.

        Returns:
            How many positions were closed.
        """
        closed = 0
        for position in self.broker.positions(self.cfg.execution.magic):
            result = self.broker.close(position.ticket, None, reason)
            if result.ok:
                self._settle(position.ticket)
                closed += 1
        return closed


def _with_initial_stop(trade: ClosedTrade, initial_stop: float) -> ClosedTrade:
    """Return ``trade`` with its initial stop and R multiple filled in from the journal."""
    if trade.initial_stop:
        return trade
    risk = abs(trade.entry_price - initial_stop)
    r = 0.0
    if risk > 0.0:
        direction = 1.0 if trade.side is Side.BUY else -1.0
        r = direction * (trade.exit_price - trade.entry_price) / risk
    return replace(trade, initial_stop=initial_stop, r_multiple=r)


def replay_history(
    cfg: AppConfig,
    clock: ServerClock,
    frames: Mapping[str, pd.DataFrame],
    specs: Mapping[str, SymbolSpec],
    governor_factory: Callable[[Journal], RiskGovernor],
    journal: Journal,
    alerter: Alerter,
    starting_equity: float = 10_000.0,
    spread_multiplier: float = 1.0,
    slippage_multiplier: float = 1.0,
    start_index: int | None = None,
) -> RunResult:
    """Drive this engine over a fixed history with the paper broker.

    Lives here rather than in ``backtest/`` because §2.1 forbids ``backtest/`` from
    importing ``runtime.engine``: the arrow points inward, always.

    This is the other half of the parity contract, and it is also what ``dry_run`` uses
    against a live feed in §13.5 step 2: the same engine, the same governor, the same
    intents -- only the broker is simulated.

    Args:
        cfg: The resolved configuration.
        clock: The broker clock.
        frames: ``symbol -> ascending bar frame``.
        specs: ``symbol -> SymbolSpec``.
        governor_factory: Callable taking the journal and returning a loaded governor.
        journal: The journal the engine writes to.
        alerter: The alerter.
        starting_equity: Opening balance.
        spread_multiplier: §11.2 stress knob.
        slippage_multiplier: §11.2 stress knob.
        start_index: First bar index to decide on. Defaults to ``warmup_bars - 1``, which
            is the first bar at which the frame holds ``warmup_bars`` rows -- the same bar
            Backtrader first clears the warmup gate on. Starting one bar later would give
            the two engines different first decisions and a histogram that never matches.

    Returns:
        The :class:`RunResult`.
    """
    assert_swaps_modelled(specs)
    cfg = narrow_to(cfg, list(frames))
    bars = {symbol: frame_to_bars(frame) for symbol, frame in frames.items()}
    broker = PaperBroker(cfg, specs, bars, starting_equity, "USD",
                         spread_multiplier, slippage_multiplier)
    source = ReplayDataSource(frames, specs, clock, broker, cfg.timeframe_minutes)
    governor = governor_factory(journal)
    health = Health(None, cfg.timeframe_minutes, cfg.runtime.watchdog_multiples)
    engine = TradingEngine(cfg, clock, source, broker, governor, journal, alerter, health)

    length = min(len(series) for series in bars.values())
    first = (cfg.strategy.warmup_bars - 1) if start_index is None else start_index
    equity_curve: list[tuple[datetime, float]] = []

    for index in range(first, length):
        for symbol in frames:
            broker.on_bar(symbol, index)
        source.seek_all(index)
        engine.run_cycle()
        account = source.account()
        equity_curve.append((account.server_time, account.equity))

    histogram = journal.reject_histogram()
    report = build_report(broker.closed_trades, equity_curve, starting_equity,
                          cfg.timeframe_minutes, histogram, journal.entry_regimes())
    return RunResult(broker.closed_trades, equity_curve, report, histogram)
