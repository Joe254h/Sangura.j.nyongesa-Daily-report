"""Simulated broker for the backtest and for ``dry_run`` against a live feed.

This module is the **single documented exception** to the import rules (§2.1, §12.5): it
imports :mod:`fxbot.backtest.costs`, because both engines must share exactly one fill
model or the keystone parity test can never pass. ``tests/test_layering.py`` asserts the
edge as an allow-listed exception rather than ignoring it.

The bar protocol, which the harness must honour and which mirrors Backtrader's own order
timing exactly:

1. the engine decides on the close of bar ``i`` and calls :meth:`PaperBroker.open`,
   :meth:`close` or :meth:`modify_stop`; every market order fills at the open of bar
   ``i + 1`` and every stop becomes active from bar ``i + 1``;
2. the harness then calls :meth:`on_bar` for bar ``i + 1``, which tests the active stops
   against that bar -- *after* the market fills, in submission order, exactly as
   Backtrader's pending-order queue does.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from fxbot.backtest.costs import AccountBook, FillModel, floating_pnl, realised_pnl
from fxbot.config.schema import AppConfig
from fxbot.core.enums import Side
from fxbot.core.models import (
    AccountState,
    Bar,
    ClosedTrade,
    OrderResult,
    Position,
    SizedOrder,
    SymbolSpec,
)
from fxbot.execution.retry import TRADE_RETCODE_DONE, TRADE_RETCODE_REJECT

_LEVERAGE = 400
"""Reported in ``AccountState`` for completeness; sizing is risk-based and ignores it."""


@dataclass
class _PaperPosition:
    """A simulated open position plus the bookkeeping the broker does not store."""

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
    magic: int = 0
    comment: str = ""
    stop_active_from: int = 0
    realised_pnl: float = 0.0
    """Banked P/L from partial closes, carried into the final ClosedTrade."""
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


class PaperBroker:
    """A deterministic simulator implementing :class:`~fxbot.execution.broker.Broker`."""

    def __init__(
        self,
        cfg: AppConfig,
        specs: Mapping[str, SymbolSpec],
        bars: Mapping[str, Sequence[Bar]],
        starting_cash: float = 10_000.0,
        currency: str = "USD",
        spread_multiplier: float = 1.0,
        slippage_multiplier: float = 1.0,
    ) -> None:
        """Build the simulator.

        Args:
            cfg: The resolved configuration; ``cfg.costs`` drives the fill model.
            specs: ``symbol -> SymbolSpec``, ideally captured from the live broker.
            bars: ``symbol -> full ascending bar series``. The simulator needs the *next*
                bar to fill a market order at its open, which is what makes ``open()``
                synchronous and keeps the live engine's contract unchanged.
            starting_cash: Opening balance.
            currency: Account currency.
            spread_multiplier: §11.2 stress knob.
            slippage_multiplier: §11.2 stress knob.
        """
        self._cfg = cfg
        self._specs = dict(specs)
        self._bars = {sym: list(series) for sym, series in bars.items()}
        self._index: dict[str, int] = dict.fromkeys(self._bars, -1)
        self.book = AccountBook(starting_cash, currency)
        self.models: dict[str, FillModel] = {
            sym: FillModel(
                spec=spec,
                commission_per_lot_per_side=cfg.costs.commission_per_lot_per_side,
                slippage_points=cfg.costs.slippage(sym),
                spread_source=cfg.costs.spread_source,
                fixed_spread_points=cfg.costs.fixed_spread(sym),
                spread_multiplier=spread_multiplier,
                slippage_multiplier=slippage_multiplier,
            )
            for sym, spec in self._specs.items()
        }
        self._positions: dict[int, _PaperPosition] = {}
        self._closed: list[ClosedTrade] = []
        self._next_ticket = 1
        self._server_time: datetime | None = None

    # ------------------------------------------------------------------ harness API

    @property
    def closed_trades(self) -> list[ClosedTrade]:
        """Every completed round trip, in close order."""
        return list(self._closed)

    def bar_index(self, symbol: str) -> int:
        """Return the index of the last bar :meth:`on_bar` was given for ``symbol``."""
        return self._index.get(symbol, -1)

    def on_bar(self, symbol: str, index: int) -> list[ClosedTrade]:
        """Advance ``symbol`` to bar ``index`` and test the active stops against it.

        Market orders queued for this bar have already filled at its open inside
        :meth:`open` / :meth:`close`, exactly as Backtrader executes pending market orders
        before it reaches the stop orders behind them in the queue.

        Args:
            symbol: The symbol to advance.
            index: The index of the newly closed bar.

        Returns:
            Trades closed by a stop on this bar.
        """
        self._index[symbol] = index
        bar = self._bars[symbol][index]
        self._server_time = bar.time
        closed: list[ClosedTrade] = []
        model = self.models[symbol]

        for pos in [p for p in self._positions.values() if p.symbol == symbol]:
            self._update_excursions(pos, bar)
            if index < pos.stop_active_from:
                continue
            fill = model.stop_fill(pos.side, bar, pos.stop_loss)
            if fill is not None:
                closed.append(self._settle(pos, fill, bar.time, "stop"))
        for pos in [p for p in self._positions.values() if p.symbol == symbol]:
            pos.mark = bar.close
        return closed

    def _update_excursions(self, pos: _PaperPosition, bar: Bar) -> None:
        """Track MAE/MFE in R over the bar's range."""
        pos.mae_r = min(pos.mae_r, pos.r_at(bar.low if pos.side is Side.BUY else bar.high))
        pos.mfe_r = max(pos.mfe_r, pos.r_at(bar.high if pos.side is Side.BUY else bar.low))

    def account(self) -> AccountState:
        """Return the simulated account snapshot the governor sizes from."""
        floating = sum(
            floating_pnl(p.side, p.volume, p.entry_price, p.mark or p.entry_price,
                         self._specs[p.symbol])
            for p in self._positions.values()
        )
        equity = self.book.equity(floating)
        return AccountState(
            equity=equity,
            balance=self.book.balance,
            margin=0.0,
            margin_free=equity,
            currency=self.book.currency,
            leverage=_LEVERAGE,
            server_time=self._server_time or datetime.min,
        )

    # ------------------------------------------------------------------ Broker port

    def open(self, order: SizedOrder) -> OrderResult:
        """Fill a market order at the open of the bar after the decision bar."""
        if not order.approval_id:
            raise ValueError("PaperBroker.open() refuses an order without an approval_id (§0.4)")
        symbol = order.symbol
        nxt = self._index.get(symbol, -1) + 1
        series = self._bars.get(symbol, [])
        if nxt >= len(series):
            return self._reject(order.symbol, "no next bar: end of data")

        bar = series[nxt]
        model = self.models[symbol]
        fill = model.market_fill(order.side, bar)
        commission = model.commission(order.volume)
        self.book.charge(commission)

        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions[ticket] = _PaperPosition(
            ticket=ticket,
            symbol=symbol,
            side=order.side,
            volume=order.volume,
            entry_price=fill,
            entry_time=bar.time,
            entry_index=nxt,
            stop_loss=order.stop_price,
            initial_stop=order.stop_price,
            initial_volume=order.volume,
            magic=self._cfg.execution.magic,
            comment=f"{self._cfg.execution.order_comment_prefix}|{order.approval_id[:8]}",
            # A stop submitted with the entry is first testable on the bar the entry fills
            # on -- after the fill, because it sits behind it in the queue.
            stop_active_from=nxt,
            commission_paid=commission,
            mark=bar.close,
        )
        slippage = abs(fill - bar.open) / self._specs[symbol].point
        return OrderResult(ok=True, retcode=TRADE_RETCODE_DONE, ticket=ticket,
                           filled_volume=order.volume, filled_price=fill,
                           slippage_points=slippage, comment="paper fill",
                           request_id=order.approval_id)

    def close(self, ticket: int, volume: float | None = None,
              reason: str = "manual") -> OrderResult:
        """Close a position wholly or partially at the next bar's open."""
        pos = self._positions.get(ticket)
        if pos is None:
            return self._reject("", f"unknown ticket {ticket}")
        nxt = self._index.get(pos.symbol, -1) + 1
        series = self._bars[pos.symbol]
        if nxt >= len(series):
            return self._reject(pos.symbol, "no next bar: end of data")

        bar = series[nxt]
        model = self.models[pos.symbol]
        amount = pos.volume if volume is None else min(volume, pos.volume)
        amount = self._specs[pos.symbol].floor_volume(amount)
        if amount <= 0.0:
            return self._reject(pos.symbol, "close volume rounds to zero")

        fill = model.market_fill(pos.side.opposite, bar)
        if amount >= pos.volume:
            trade = self._settle(pos, fill, bar.time, reason)
            filled = trade.volume
        else:
            filled = self._settle_partial(pos, amount, fill, model)
        return OrderResult(ok=True, retcode=TRADE_RETCODE_DONE, ticket=ticket,
                           filled_volume=filled, filled_price=fill,
                           slippage_points=abs(fill - bar.open) / self._specs[pos.symbol].point,
                           comment=f"paper close:{reason}", request_id=str(uuid.uuid4()))

    def modify_stop(self, ticket: int, stop: float,
                    take_profit: float | None) -> OrderResult:  # noqa: ARG002
        """Move a position's stop. Active from the next bar, as Backtrader's would be.

        ``take_profit`` is part of the Broker port and is always None in v1 (§7.4): the
        1.5R exit is a partial close and the runner is trailed, so there is no static
        target to set.
        """
        pos = self._positions.get(ticket)
        if pos is None:
            return self._reject("", f"unknown ticket {ticket}")
        pos.stop_loss = stop
        pos.stop_active_from = self._index[pos.symbol] + 1
        return OrderResult(ok=True, retcode=TRADE_RETCODE_DONE, ticket=ticket,
                           filled_volume=0.0, filled_price=stop, slippage_points=0.0,
                           comment="paper modify", request_id=str(uuid.uuid4()))

    def positions(self, magic: int) -> list[Position]:
        """Return the bot's open positions, ordered by ticket for determinism."""
        out: list[Position] = []
        for pos in sorted(self._positions.values(), key=lambda p: p.ticket):
            if pos.magic != magic:
                continue
            spec = self._specs[pos.symbol]
            out.append(Position(
                ticket=pos.ticket, symbol=pos.symbol, side=pos.side, volume=pos.volume,
                entry_price=pos.entry_price, stop_loss=pos.stop_loss, take_profit=0.0,
                open_time=pos.entry_time,
                profit=floating_pnl(pos.side, pos.volume, pos.entry_price,
                                    pos.mark or pos.entry_price, spec),
                magic=pos.magic, comment=pos.comment,
                initial_stop=pos.initial_stop, initial_volume=pos.initial_volume,
                partial_taken=pos.partial_taken,
            ))
        return out

    def closed_deals(self, since: datetime, magic: int) -> list[ClosedTrade]:
        """Return completed round trips at or after ``since``."""
        return [t for t in self._closed if t.exit_time >= since and t.magic == magic]

    # ------------------------------------------------------------------ settlement

    def _settle(self, pos: _PaperPosition, price: float, when: datetime,
                reason: str) -> ClosedTrade:
        """Close ``pos`` entirely and record the trade."""
        spec = self._specs[pos.symbol]
        model = self.models[pos.symbol]
        gross_leg = realised_pnl(pos.side, pos.volume, pos.entry_price, price, spec)
        commission = model.commission(pos.volume)
        swap = model.swap(pos.side, pos.initial_volume, pos.entry_time, when)
        self.book.charge(commission)
        self.book.credit(gross_leg + swap)

        gross_total = gross_leg + pos.realised_pnl
        commission_total = pos.commission_paid + commission
        net = gross_total + swap - commission_total
        risk = abs(pos.entry_price - pos.initial_stop) * spec.value_per_price_unit_per_lot
        r = net / (risk * pos.initial_volume) if risk > 0.0 else 0.0

        trade = ClosedTrade(
            ticket=pos.ticket, symbol=pos.symbol, side=pos.side, volume=pos.initial_volume,
            entry_price=pos.entry_price, exit_price=price,
            entry_time=pos.entry_time, exit_time=when, initial_stop=pos.initial_stop,
            gross_pnl=gross_total, commission=commission_total, swap=swap, net_pnl=net,
            r_multiple=r, mae_r=pos.mae_r, mfe_r=pos.mfe_r, exit_reason=reason,
            magic=pos.magic,
        )
        self._closed.append(trade)
        del self._positions[pos.ticket]
        return trade

    def _settle_partial(self, pos: _PaperPosition, amount: float, price: float,
                        model: FillModel) -> float:
        """Bank part of ``pos``, leaving the runner open."""
        spec = self._specs[pos.symbol]
        gross = realised_pnl(pos.side, amount, pos.entry_price, price, spec)
        commission = model.commission(amount)
        self.book.charge(commission)
        self.book.credit(gross)
        pos.realised_pnl += gross
        pos.commission_paid += commission
        pos.volume = round(pos.volume - amount, 8)
        # initial_stop and initial_volume are frozen at open and never updated, including
        # after a partial: R stays measured against the original risk (§7.4).
        pos.partial_taken = True
        return amount

    def _reject(self, symbol: str, detail: str) -> OrderResult:
        """Build a rejected :class:`OrderResult`."""
        return OrderResult(ok=False, retcode=TRADE_RETCODE_REJECT, ticket=None,
                           filled_volume=0.0, filled_price=0.0, slippage_points=0.0,
                           comment=f"{symbol}: {detail}", request_id=str(uuid.uuid4()))

    def ensure_connected(self) -> None:
        """No-op: the simulator is always connected.

        Present so the engine can treat both brokers identically.
        """
        return
