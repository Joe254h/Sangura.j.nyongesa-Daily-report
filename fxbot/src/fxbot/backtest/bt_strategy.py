"""The Backtrader adapter (§11.1). **Zero trading logic.**

``next()`` builds a :class:`~fxbot.core.models.StrategyContext` from the bars Backtrader
has delivered so far, calls the same :func:`~fxbot.strategy.trend_donchian.generate_signal`
and :func:`~fxbot.strategy.manage.manage_position` the live engine calls, and translates
the returned :class:`~fxbot.core.models.Intent` into Backtrader orders. The real
:class:`~fxbot.risk.governor.RiskGovernor` runs here too, kill switch and all: a backtest
that ignores the daily loss limit is measuring a different system than the one you deploy.

If you find yourself about to write ``if adx > ...`` in this file, stop -- that rule
belongs in ``strategy/`` and a rule that exists only here is a bug (§0.1).

**How the context is built.** The frame is sliced from the source history at
``len(self.data)`` rather than rebuilt from Backtrader's line buffers each bar. The two are
the same bars -- Backtrader's cursor is the slice bound -- and the slice is O(1) where
rebuilding 600 rows per bar is O(n^2) over an eight-year run. ``tests/test_parity.py`` is
what proves they really are the same bars.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import backtrader as bt
import pandas as pd

from fxbot.backtest.costs import AccountBook, FillModel, floating_pnl, realised_pnl
from fxbot.config.schema import AppConfig
from fxbot.core.clock import ServerClock
from fxbot.core.enums import IntentKind, RejectReason, RiskStatus, Side
from fxbot.core.models import (
    AccountState,
    ClosedTrade,
    Position,
    QualityReport,
    StrategyContext,
    SymbolSpec,
)
from fxbot.data.resample import resample_h1_to_d1
from fxbot.risk.governor import RiskGovernor
from fxbot.strategy.manage import manage_position
from fxbot.strategy.trend_donchian import generate_signal


def _same_order(tracked: bt.Order | None, notified: bt.Order) -> bool:
    """Return whether a notification belongs to a tracked order.

    **Backtrader clones an order before notifying**, so ``tracked is notified`` is False
    even for the order you just submitted -- which silently breaks every fill handler
    written against identity. ``ref`` is the stable integer the clone carries over.
    """
    return tracked is not None and tracked.ref == notified.ref


@dataclass
class _BtPosition:
    """The adapter's own record of a position; mirrors the paper broker's exactly."""

    ticket: int
    symbol: str
    side: Side
    volume: float
    entry_price: float
    entry_time: datetime
    entry_index: int
    stop_loss: float
    initial_stop: float
    initial_volume: float
    partial_taken: bool = False
    realised: float = 0.0
    commission_paid: float = 0.0
    mae_r: float = 0.0
    mfe_r: float = 0.0
    mark: float = 0.0

    def r_at(self, price: float) -> float:
        """Return the R multiple at ``price``, against the initial stop."""
        risk = (self.entry_price - self.initial_stop) * self.side.sign
        if risk == 0.0:
            return 0.0
        return (price - self.entry_price) * self.side.sign / risk


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One journalled decision, kept in memory for the report's histograms."""

    when: datetime
    """Close of the bar the decision was made on. Equals the fill bar's OPEN time, which
    is what lets the per-regime breakdown join decisions to trades without a ticket."""
    symbol: str
    reason: str
    regime: str
    side: str | None


@dataclass
class BtRunState:
    """What the adapter accumulates over a run, for the report and for the parity test."""

    trades: list[ClosedTrade] = field(default_factory=list)
    decisions: list[DecisionRecord] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)


class FxStrategy(bt.Strategy):  # type: ignore[misc]
    """Drives the pure strategy from Backtrader's bar loop."""

    params = (
        ("cfg", None),
        ("clock", None),
        ("specs", None),
        ("frames", None),
        ("governor", None),
        ("book", None),
        ("state", None),
        ("models", None),
    )

    def __init__(self) -> None:
        """Bind the injected collaborators and prepare per-symbol bookkeeping."""
        self.cfg: AppConfig = self.p.cfg
        self.clock: ServerClock = self.p.clock
        self.specs: Mapping[str, SymbolSpec] = self.p.specs
        self.frames: Mapping[str, pd.DataFrame] = self.p.frames
        self.governor: RiskGovernor = self.p.governor
        self.book: AccountBook = self.p.book
        self.state: BtRunState = self.p.state
        self.models: Mapping[str, FillModel] = self.p.models
        self._positions: dict[str, _BtPosition] = {}
        self._stop_orders: dict[str, bt.Order] = {}
        self._entry_orders: dict[str, bt.Order] = {}
        self._close_orders: dict[str, bt.Order] = {}
        self._partial_orders: dict[str, bt.Order] = {}
        self._pending_entry: dict[str, tuple[float, float, Side, int]] = {}
        self._exit_reasons: dict[str, str] = {}
        self._next_ticket = 1
        self._last_day: datetime | None = None

    # ------------------------------------------------------------------ helpers

    def _symbol(self, data: bt.DataBase) -> str:
        """Return the canonical symbol name for a data feed."""
        return str(data._name)

    def _bar_open(self, data: bt.DataBase) -> datetime:
        """Return the OPEN time of the bar just delivered, in server time.

        Trade timestamps use bar-open time on both engines: a fill happens at a bar's open,
        and ``test_parity.py`` compares entry bars, so the two must label them identically.
        """
        return self.clock.localize(bt.num2date(data.datetime[0]))

    def _bar_close(self, data: bt.DataBase) -> datetime:
        """Return the close time of the bar just delivered, in server time."""
        return self._bar_open(data) + timedelta(minutes=self.cfg.timeframe_minutes)

    def _account(self, marks: Mapping[str, float]) -> AccountState:
        """Return the account snapshot the governor sizes from."""
        floating = sum(
            floating_pnl(p.side, p.volume, p.entry_price, marks.get(p.symbol, p.mark),
                         self.specs[p.symbol])
            for p in self._positions.values()
        )
        equity = self.book.equity(floating)
        when = self._bar_close(self.datas[0])
        return AccountState(equity=equity, balance=self.book.balance, margin=0.0,
                            margin_free=equity, currency=self.book.currency,
                            leverage=400, server_time=when)

    def _context(self, data: bt.DataBase) -> StrategyContext:
        """Build the strategy context for one feed at the current bar."""
        symbol = self._symbol(data)
        # The SAME rolling window the live engine fetches, not an expanding one.
        # `MT5DataSource.bars()` asks for `context_bars + 5` closed bars, so the EMA/ATR/ADX
        # recursions are seeded at a fixed offset behind the current bar. Slicing from bar
        # 0 here instead would seed them at a different point, and the two engines'
        # indicators would drift apart over a long run -- silently, until some bar late in
        # the history falls on the other side of a threshold. `test_parity.py` caught
        # exactly that at bar ~2,100 of the fixture.
        end = len(data)
        window = self.cfg.strategy.context_bars + 5
        frame = self.frames[symbol].iloc[max(0, end - window):end]
        spec = self.specs[symbol]
        now = self._bar_close(data)
        spread_points = int(data.spread[0]) if len(data.spread) else 0
        report = QualityReport(ok=True, reason=RejectReason.NONE, detail="", bars=len(frame),
                               last_close=now, gap_count=0, fatal_sanity=False)
        if end < self.cfg.strategy.warmup_bars:
            report = QualityReport(ok=False, reason=RejectReason.STALE_DATA, detail="warmup",
                                   bars=len(frame), last_close=now, gap_count=0,
                                   fatal_sanity=False)
        record = self._positions.get(symbol)
        position = None if record is None else Position(
            ticket=record.ticket, symbol=symbol, side=record.side, volume=record.volume,
            entry_price=record.entry_price, stop_loss=record.stop_loss, take_profit=0.0,
            open_time=record.entry_time,
            profit=floating_pnl(record.side, record.volume, record.entry_price,
                                float(data.close[0]), spec),
            magic=self.cfg.execution.magic, comment="bt", initial_stop=record.initial_stop,
            initial_volume=record.initial_volume, partial_taken=record.partial_taken,
        )
        return StrategyContext(
            symbol=symbol, now=now, h1=frame,
            d1=resample_h1_to_d1(frame, self.clock, now), spec=spec,
            current_spread_points=spread_points, open_position=position,
            params=self.cfg.strategy, session=self.cfg.session,
            max_spread_points=self.cfg.execution.spread_cap(symbol),
            commission_per_lot_round_turn=self.cfg.costs.commission_per_lot_round_turn,
            quality=report,
        )

    # ------------------------------------------------------------------ the loop

    def next(self) -> None:
        """One bar: reconcile fills, refresh risk, manage, then consider entries."""
        marks = {self._symbol(d): float(d.close[0]) for d in self.datas}
        for symbol, record in self._positions.items():
            record.mark = marks.get(symbol, record.mark)
            self._update_excursions(record, symbol)

        account = self._account(marks)
        self.clock.observe(account.server_time)
        if self._last_day is not None and self.clock.is_new_trading_day(
                self._last_day, account.server_time):
            self.governor.on_new_day(account)
        self._last_day = account.server_time

        positions = [self._as_position(r, marks) for r in self._positions.values()]
        status = self.governor.refresh(account, positions)
        if len(self.datas[0]) >= self.cfg.strategy.warmup_bars:
            # §11.5: discard the warmup bars from results. Sampling equity from bar 0 would
            # report a different Sharpe and exposure than the replay engine for an
            # identical set of trades.
            self.state.equity_curve.append((account.server_time, account.equity))

        if status is not RiskStatus.HALTED:
            for data in self.datas:
                symbol = self._symbol(data)
                if symbol in self._positions:
                    self._manage(data)

        if status in (RiskStatus.DAILY_LOCKOUT, RiskStatus.HALTED):
            return

        for data in self.datas:
            symbol = self._symbol(data)
            if symbol in self._positions or symbol in self._pending_entry:
                continue
            self._consider_entry(data, account, positions)
            positions = [self._as_position(r, marks) for r in self._positions.values()]

    def _as_position(self, record: _BtPosition, marks: Mapping[str, float]) -> Position:
        """Project the adapter's record onto the shared Position contract."""
        spec = self.specs[record.symbol]
        return Position(
            ticket=record.ticket, symbol=record.symbol, side=record.side,
            volume=record.volume, entry_price=record.entry_price, stop_loss=record.stop_loss,
            take_profit=0.0, open_time=record.entry_time,
            profit=floating_pnl(record.side, record.volume, record.entry_price,
                                marks.get(record.symbol, record.mark), spec),
            magic=self.cfg.execution.magic, comment="bt", initial_stop=record.initial_stop,
            initial_volume=record.initial_volume, partial_taken=record.partial_taken,
        )

    def _update_excursions(self, record: _BtPosition, symbol: str) -> None:
        """Track MAE/MFE in R across the bar's range."""
        data = self.getdatabyname(symbol)
        high, low = float(data.high[0]), float(data.low[0])
        record.mae_r = min(record.mae_r,
                           record.r_at(low if record.side is Side.BUY else high))
        record.mfe_r = max(record.mfe_r,
                           record.r_at(high if record.side is Side.BUY else low))

    def _consider_entry(self, data: bt.DataBase, account: AccountState,
                        positions: list[Position]) -> None:
        """Evaluate one feed for an entry and translate the verdict into orders."""
        ctx = self._context(data)
        if not ctx.quality.ok:
            # Mirror the live engine exactly: `build_context(strict=True)` returns None on
            # a quality failure and `_consider_entry` returns before journalling anything
            # (§10.2 step 9). Recording a warmup bar here would put rows in one engine's
            # reject histogram that the other never produces.
            return
        signal = generate_signal(ctx)
        self.state.decisions.append(DecisionRecord(
            when=ctx.now, symbol=ctx.symbol, reason=str(signal.reason),
            regime=str(signal.regime), side=None if signal.side is None else str(signal.side),
        ))
        if signal.side is None:
            return
        approval = self.governor.approve(signal, ctx, account, positions)
        if not approval.ok or approval.order is None:
            return

        order = approval.order
        size = order.volume
        # The entry and its protective stop are submitted together, so the stop is live on
        # the bar the entry fills on -- behind it in Backtrader's queue, which is exactly
        # the ordering `PaperBroker` reproduces (see costs.py).
        if order.side is Side.BUY:
            entry = self.buy(data=data, size=size, exectype=bt.Order.Market)
            stop = self.sell(data=data, size=size, exectype=bt.Order.Stop,
                             price=order.stop_price)
        else:
            entry = self.sell(data=data, size=size, exectype=bt.Order.Market)
            stop = self.buy(data=data, size=size, exectype=bt.Order.Stop,
                            price=order.stop_price)
        symbol = ctx.symbol
        self._entry_orders[symbol] = entry
        self._stop_orders[symbol] = stop
        # The submission bar index goes with it: the fill lands on the NEXT bar, and that
        # is what timestamps the trade. See `_open_position`.
        self._pending_entry[symbol] = (order.stop_price, size, order.side, len(data) - 1)

    def _manage(self, data: bt.DataBase) -> None:
        """Run the exit logic for one open position and translate its single intent."""
        ctx = self._context(data)
        intent = manage_position(ctx)
        symbol = ctx.symbol
        record = self._positions[symbol]

        if intent.kind is IntentKind.NONE:
            return

        if intent.kind is IntentKind.CLOSE:
            self._cancel_stop(symbol)
            self._close_orders[symbol] = self.close(data=data)
            self._exit_reasons[symbol] = intent.reason
            return

        if intent.kind is IntentKind.CLOSE_PARTIAL:
            fraction = intent.close_fraction or 0.0
            volume = ctx.spec.floor_volume(record.volume * fraction)
            if volume <= 0.0:
                return
            self._cancel_stop(symbol)
            remaining = round(record.volume - volume, 8)
            if record.side is Side.BUY:
                partial = self.sell(data=data, size=volume, exectype=bt.Order.Market)
                stop = self.sell(data=data, size=remaining, exectype=bt.Order.Stop,
                                 price=record.stop_loss)
            else:
                partial = self.buy(data=data, size=volume, exectype=bt.Order.Market)
                stop = self.buy(data=data, size=remaining, exectype=bt.Order.Stop,
                                price=record.stop_loss)
            self._partial_orders[symbol] = partial
            self._stop_orders[symbol] = stop
            self._exit_reasons[symbol] = intent.reason
            return

        if intent.kind is IntentKind.MODIFY_STOP and intent.stop_price is not None:
            self._cancel_stop(symbol)
            record.stop_loss = intent.stop_price
            if record.side is Side.BUY:
                stop = self.sell(data=data, size=record.volume, exectype=bt.Order.Stop,
                                 price=intent.stop_price)
            else:
                stop = self.buy(data=data, size=record.volume, exectype=bt.Order.Stop,
                                price=intent.stop_price)
            self._stop_orders[symbol] = stop

    def _cancel_stop(self, symbol: str) -> None:
        """Cancel the live stop order for ``symbol``, if any."""
        order = self._stop_orders.pop(symbol, None)
        if order is not None and order.alive():
            self.cancel(order)

    # ------------------------------------------------------------------ fills

    def notify_order(self, order: bt.Order) -> None:
        """Turn a Backtrader fill into the adapter's own position bookkeeping.

        Every price and every cost here comes from the shared model, so the numbers this
        produces are the same numbers ``PaperBroker`` produces from the same bars.
        """
        if order.status in (order.Submitted, order.Accepted):
            return
        symbol = self._symbol(order.data)
        if order.status in (order.Canceled, order.Margin, order.Rejected, order.Expired):
            if _same_order(self._entry_orders.get(symbol), order):
                # The entry never reached the market: release the symbol so the next bar
                # can consider it again, rather than blocking it for the rest of the run.
                self._entry_orders.pop(symbol, None)
                self._pending_entry.pop(symbol, None)
                self._cancel_stop(symbol)
            return
        if order.status is not order.Completed:
            return

        price = float(order.executed.price)
        # `executed.dt` is when the order actually executed. Reading the feed's current bar
        # instead labels the fill with whatever bar the notification happens to be
        # delivered on, which is one bar late whenever Backtrader defers it -- and a trade
        # stamped an hour off has the wrong swap, the wrong holding period, and fails the
        # parity comparison on entry bars.
        when = self.clock.localize(bt.num2date(order.executed.dt))

        if _same_order(self._entry_orders.get(symbol), order):
            self._open_position(symbol, order, price, when)
            return
        if _same_order(self._stop_orders.get(symbol), order):
            self._stop_orders.pop(symbol, None)
            self._settle(symbol, price, when, "stop")
            return
        if _same_order(self._partial_orders.get(symbol), order):
            self._partial_orders.pop(symbol, None)
            self._settle_partial(symbol, abs(float(order.executed.size)), price)
            return
        if _same_order(self._close_orders.get(symbol), order):
            self._close_orders.pop(symbol, None)
            self._settle(symbol, price, when, self._exit_reasons.pop(symbol, "manual"))

    def _open_position(self, symbol: str, order: bt.Order, price: float,
                       when: datetime) -> None:
        """Record a filled entry.

        **The timestamp comes from the submission bar, not from Backtrader.** A market
        order submitted on bar ``i`` fills at bar ``i + 1``'s open -- that is the contract
        ``costs.py`` states and ``PaperBroker`` implements. Backtrader's own
        ``executed.dt`` (and the feed's current bar at notification time) is occasionally
        one bar later than the bar it actually priced the fill from: on the fixture it
        stamps one entry 08:00 while filling at the 07:00 bar's open. The execution price
        proves which bar was used, so the index is the honest label and the one both
        engines agree on. ``test_parity.py`` compares entry bars, so this is not cosmetic.
        """
        self._entry_orders.pop(symbol, None)
        stop_price, size, _side, submitted_at = self._pending_entry.pop(
            symbol, (0.0, 0.0, Side.BUY, 0))
        frame = self.frames[symbol]
        fill_index = min(submitted_at + 1, len(frame) - 1)
        when = self.clock.localize(frame.index[fill_index].to_pydatetime().replace(tzinfo=None))
        side = Side.BUY if order.isbuy() else Side.SELL
        model = self.models[symbol]
        commission = model.commission(size)
        self.book.charge(commission)
        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions[symbol] = _BtPosition(
            ticket=ticket, symbol=symbol, side=side, volume=size, entry_price=price,
            entry_time=when, entry_index=len(order.data), stop_loss=stop_price,
            initial_stop=stop_price, initial_volume=size, commission_paid=commission,
            mark=price,
        )

    def _settle_partial(self, symbol: str, amount: float, price: float) -> None:
        """Bank part of a position, leaving the runner open."""
        record = self._positions.get(symbol)
        if record is None:
            return
        spec = self.specs[symbol]
        model = self.models[symbol]
        gross = realised_pnl(record.side, amount, record.entry_price, price, spec)
        commission = model.commission(amount)
        self.book.charge(commission)
        self.book.credit(gross)
        record.realised += gross
        record.commission_paid += commission
        record.volume = round(record.volume - amount, 8)
        # initial_stop and initial_volume stay frozen: R is measured against the original
        # risk, before and after the partial (§7.4).
        record.partial_taken = True

    def _settle(self, symbol: str, price: float, when: datetime, reason: str) -> None:
        """Close a position entirely, record the trade and tell the governor."""
        record = self._positions.pop(symbol, None)
        if record is None:
            return
        self._cancel_stop(symbol)
        self._close_orders.pop(symbol, None)
        spec = self.specs[symbol]
        model = self.models[symbol]

        gross_leg = realised_pnl(record.side, record.volume, record.entry_price, price, spec)
        commission = model.commission(record.volume)
        swap = model.swap(record.side, record.initial_volume, record.entry_time, when)
        self.book.charge(commission)
        self.book.credit(gross_leg + swap)

        gross_total = gross_leg + record.realised
        commission_total = record.commission_paid + commission
        net = gross_total + swap - commission_total
        risk = abs(record.entry_price - record.initial_stop) * spec.value_per_price_unit_per_lot
        r = net / (risk * record.initial_volume) if risk > 0.0 else 0.0

        trade = ClosedTrade(
            ticket=record.ticket, symbol=symbol, side=record.side,
            volume=record.initial_volume, entry_price=record.entry_price, exit_price=price,
            entry_time=record.entry_time, exit_time=when, initial_stop=record.initial_stop,
            gross_pnl=gross_total, commission=commission_total, swap=swap, net_pnl=net,
            r_multiple=r, mae_r=record.mae_r, mfe_r=record.mfe_r, exit_reason=reason,
            magic=self.cfg.execution.magic,
        )
        self.state.trades.append(trade)
        self.governor.record_closed_trade(trade)
