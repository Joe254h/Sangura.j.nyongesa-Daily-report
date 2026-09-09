"""Live MetaTrader 5 broker (§9.2 - §9.5).

Market orders only in v1: pending orders add an entire state machine for marginal benefit
at H1. Every send runs the full pre-send sequence, in order, and **never assumes a
non-exception means a fill** (§17.11).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from fxbot.config.schema import AppConfig
from fxbot.core.clock import ServerClock
from fxbot.core.enums import Side
from fxbot.core.errors import BrokerConnectionError
from fxbot.core.models import ClosedTrade, OrderResult, Position, SizedOrder, SymbolSpec
from fxbot.data.mt5_source import MT5DataSource, require_mt5, server_time_from_epoch
from fxbot.execution.filling import FillingCache, filling_candidates
from fxbot.execution.retry import (
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_PLACED,
    TRADE_RETCODE_REJECT,
    Action,
    backoff_delays,
    classify,
)
from fxbot.risk.sizing import position_size

MAX_TICK_AGE_S = 5.0
"""A tick older than this is stale; abort rather than send against a dead quote (§9.2)."""
_POLL_SECONDS = 5.0
_POLL_INTERVAL_S = 0.25
_IDEMPOTENCY_WINDOW_S = 60.0
_COMMENT_MAX = 31

_DEAL_ENTRY_OUT = 1
"""``DEAL_ENTRY_OUT`` -- the closing leg of a position."""

log = structlog.get_logger(__name__)


class MT5Broker:
    """Implements :class:`~fxbot.execution.broker.Broker` against a running terminal."""

    def __init__(
        self,
        cfg: AppConfig,
        source: MT5DataSource,
        clock: ServerClock,
        filling_cache: FillingCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build the broker.

        Args:
            cfg: The resolved configuration.
            source: The connected data source; ticks and specs come from it.
            clock: The broker clock.
            filling_cache: Where the negotiated filling mode is remembered. Keyed by
                ``(server, symbol)`` so a demo cache is never carried into live (§9.3).
            sleep: Injected so tests do not actually wait out the backoff.
        """
        self._cfg = cfg
        self._source = source
        self._clock = clock
        self._sleep = sleep
        self._filling = filling_cache or FillingCache(
            Path(cfg.paths.filling_cache_path), cfg.env
        )
        self.last_request: dict[str, Any] = {}
        self.last_result: Any = None

    # ------------------------------------------------------------------ helpers

    def ensure_connected(self) -> None:
        """Raise if the terminal connection has gone away."""
        api = require_mt5()
        if api.terminal_info() is None:
            raise BrokerConnectionError(f"terminal_info() is None: {api.last_error()}")

    def order_calc_margin(self, order: SizedOrder) -> float:
        """Return the broker's own margin requirement for ``order`` (§8.3).

        Never computed from leverage here: the broker's number is authoritative and
        accounts for its own rules.

        Args:
            order: The candidate order.

        Returns:
            The required margin in account currency.

        Raises:
            BrokerConnectionError: If the terminal refuses the calculation -- failing
                closed beats approving an order whose margin is unknown.
        """
        api = require_mt5()
        spec = self._source.symbol_spec(order.symbol)
        bid, ask, _ = self._source.tick(spec.name)
        order_type = api.ORDER_TYPE_BUY if order.side is Side.BUY else api.ORDER_TYPE_SELL
        price = ask if order.side is Side.BUY else bid
        required = api.order_calc_margin(order_type, spec.name, order.volume, price)
        if required is None:
            raise BrokerConnectionError(
                f"order_calc_margin({spec.name}) returned None: {api.last_error()}")
        return float(required)

    # ------------------------------------------------------------------ Broker port

    def open(self, order: SizedOrder) -> OrderResult:
        """Send a market order, running the mandatory pre-send sequence first (§9.2).

        Args:
            order: A governor-approved order. An order without an ``approval_id`` is
                refused outright: there is no code path that sends one (§0.4, §17.9).

        Returns:
            The :class:`~fxbot.core.models.OrderResult`. ``ok`` is True only when the
            retcode is 10009 **and** the position was found afterwards.
        """
        if not order.approval_id:
            raise ValueError("MT5Broker.open() refuses an order without an approval_id (§0.4)")
        api = require_mt5()
        spec = self._source.symbol_spec(order.symbol)

        if self._already_delivered(order, spec.name):
            return OrderResult(ok=False, retcode=TRADE_RETCODE_REJECT, ticket=None,
                               filled_volume=0.0, filled_price=0.0, slippage_points=0.0,
                               comment="idempotency: an order for this approval already exists",
                               request_id=order.approval_id)

        candidates = self._candidates(spec)
        attempt = 0
        stop_price = order.stop_price
        delays = backoff_delays(self._cfg.execution.max_retries,
                                self._cfg.execution.retry_backoff_s)

        while True:
            # 1. Fresh tick, and refuse a stale one.
            bid, ask, tick_time = self._source.tick(spec.name)
            age = abs((self._clock.now() - tick_time).total_seconds())
            if age > MAX_TICK_AGE_S:
                return self._fail(order, f"tick is {age:.1f}s old (limit {MAX_TICK_AGE_S}s)")

            # 2. Re-check the spread: spreads blow out between signal and send.
            spread_points = int(round((ask - bid) / spec.point))
            cap = self._cfg.execution.spread_cap(order.symbol)
            if spread_points > cap:
                return self._fail(order, f"spread {spread_points} > cap {cap} at send time")

            price = ask if order.side is Side.BUY else bid

            # 3. Re-validate the stop against stops_level using the CURRENT price.
            stop_price = self._validated_stop(order, price, spec, spread_points)

            # 4. Re-size from the fresh tick. Never send a size the governor did not
            #    approve: a fill one bar later at a worse price changes the stop distance,
            #    and a differently-sized order is a different risk decision.
            resized = position_size(
                equity=self._source.account().equity,
                risk_pct=order.risk_pct if order.risk_pct > 0 else
                self._cfg.risk.risk_per_trade_pct,
                entry_price=price,
                stop_price=stop_price,
                spec=spec,
                commission_per_lot_round_turn=self._cfg.costs.commission_per_lot_round_turn,
            )
            if abs(resized.volume - order.volume) > spec.volume_step + 1e-9:
                return self._fail(
                    order,
                    f"re-size from fresh tick gives {resized.volume} vs approved "
                    f"{order.volume}: aborting rather than sending an unapproved size")

            request = {
                "action": api.TRADE_ACTION_DEAL,
                "symbol": spec.name,
                "volume": order.volume,
                "type": api.ORDER_TYPE_BUY if order.side is Side.BUY else api.ORDER_TYPE_SELL,
                "price": price,
                "sl": stop_price,
                "tp": order.take_profit or 0.0,
                "deviation": self._cfg.execution.deviation_points,
                "magic": self._cfg.execution.magic,
                "comment": self._comment(order.approval_id),
                "type_time": api.ORDER_TIME_GTC,
                "type_filling": candidates[0],
            }
            self.last_request = dict(request)

            # 5. order_check catches margin, filling and stop-level errors without
            #    touching the market.
            check = api.order_check(request)
            if check is not None and check.retcode != 0:
                decision = classify(check.retcode)
                if decision.action is Action.RETRY_NEXT_FILLING and len(candidates) > 1:
                    candidates = candidates[1:]
                    continue
                return self._fail(order, f"order_check refused: {check.retcode} {check.comment}",
                                  retcode=check.retcode)

            # 6. Send.
            result = api.order_send(request)
            self.last_result = result
            if result is None:
                return self._fail(order, f"order_send returned None: {api.last_error()}")

            # 7. Verify. Anything but 10009 is a failure; a non-exception is not a fill.
            if result.retcode == TRADE_RETCODE_DONE:
                self._filling.remember(order.symbol, candidates[0])
                return self._confirm(order, result, price, spec)
            if result.retcode == TRADE_RETCODE_PLACED:
                confirmed = self._poll_for_position(order, spec.name)
                if confirmed is not None:
                    self._filling.remember(order.symbol, candidates[0])
                    return confirmed
                return self._fail(order, "accepted but never filled within the poll window",
                                  retcode=result.retcode)

            decision = classify(result.retcode)
            if decision.action is Action.RETRY_NEXT_FILLING and len(candidates) > 1:
                candidates = candidates[1:]
                continue
            if not decision.retryable or attempt >= len(delays):
                return self._fail(order, f"{decision.detail}: {result.comment}",
                                  retcode=result.retcode)
            self._sleep(delays[attempt])
            attempt += 1

    def close(self, ticket: int, volume: float | None = None,
              reason: str = "manual") -> OrderResult:
        """Close a position, wholly or partially, at market."""
        api = require_mt5()
        found = api.positions_get(ticket=ticket)
        if not found:
            return OrderResult(ok=False, retcode=TRADE_RETCODE_REJECT, ticket=ticket,
                               filled_volume=0.0, filled_price=0.0, slippage_points=0.0,
                               comment=f"no open position with ticket {ticket}",
                               request_id=str(uuid.uuid4()))
        raw = found[0]
        spec = self._source.symbol_spec(raw.symbol)
        amount = spec.floor_volume(raw.volume if volume is None else min(volume, raw.volume))
        if amount <= 0.0:
            return OrderResult(ok=False, retcode=TRADE_RETCODE_REJECT, ticket=ticket,
                               filled_volume=0.0, filled_price=0.0, slippage_points=0.0,
                               comment="close volume rounds to zero",
                               request_id=str(uuid.uuid4()))

        bid, ask, _ = self._source.tick(spec.name)
        closing_buy = raw.type == api.POSITION_TYPE_SELL
        request = {
            "action": api.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": amount,
            "type": api.ORDER_TYPE_BUY if closing_buy else api.ORDER_TYPE_SELL,
            "position": ticket,
            "price": ask if closing_buy else bid,
            "deviation": self._cfg.execution.deviation_points,
            "magic": self._cfg.execution.magic,
            "comment": self._comment(reason),
            "type_time": api.ORDER_TIME_GTC,
            "type_filling": self._candidates(spec)[0],
        }
        self.last_request = dict(request)
        result = api.order_send(request)
        self.last_result = result
        if result is None:
            return OrderResult(ok=False, retcode=TRADE_RETCODE_REJECT, ticket=ticket,
                               filled_volume=0.0, filled_price=0.0, slippage_points=0.0,
                               comment=f"order_send returned None: {api.last_error()}",
                               request_id=str(uuid.uuid4()))
        ok = result.retcode == TRADE_RETCODE_DONE
        return OrderResult(ok=ok, retcode=int(result.retcode), ticket=ticket,
                           filled_volume=float(getattr(result, "volume", 0.0)),
                           filled_price=float(getattr(result, "price", 0.0)),
                           slippage_points=0.0, comment=str(result.comment),
                           request_id=str(uuid.uuid4()))

    def modify_stop(self, ticket: int, stop: float, take_profit: float | None) -> OrderResult:
        """Move a position's server-side stop."""
        api = require_mt5()
        found = api.positions_get(ticket=ticket)
        if not found:
            return OrderResult(ok=False, retcode=TRADE_RETCODE_REJECT, ticket=ticket,
                               filled_volume=0.0, filled_price=0.0, slippage_points=0.0,
                               comment=f"no open position with ticket {ticket}",
                               request_id=str(uuid.uuid4()))
        raw = found[0]
        request = {
            "action": api.TRADE_ACTION_SLTP,
            "symbol": raw.symbol,
            "position": ticket,
            "sl": stop,
            "tp": take_profit or 0.0,
            "magic": self._cfg.execution.magic,
        }
        self.last_request = dict(request)
        result = api.order_send(request)
        self.last_result = result
        if result is None:
            return OrderResult(ok=False, retcode=TRADE_RETCODE_REJECT, ticket=ticket,
                               filled_volume=0.0, filled_price=0.0, slippage_points=0.0,
                               comment=f"order_send returned None: {api.last_error()}",
                               request_id=str(uuid.uuid4()))
        return OrderResult(ok=result.retcode == TRADE_RETCODE_DONE, retcode=int(result.retcode),
                           ticket=ticket, filled_volume=0.0, filled_price=stop,
                           slippage_points=0.0, comment=str(result.comment),
                           request_id=str(uuid.uuid4()))

    def positions(self, magic: int) -> list[Position]:
        """Return the bot's open positions.

        ``positions_get`` accepts only ``symbol`` / ``group`` / ``ticket`` -- **there is
        no magic parameter** -- so the magic filter happens in Python (§8.6). The same is
        true of ``history_deals_get``.
        """
        api = require_mt5()
        raw = api.positions_get()
        if raw is None:
            return []
        out: list[Position] = []
        for item in raw:
            if item.magic != magic:
                continue
            out.append(Position(
                ticket=int(item.ticket), symbol=str(item.symbol),
                side=Side.BUY if item.type == api.POSITION_TYPE_BUY else Side.SELL,
                volume=float(item.volume), entry_price=float(item.price_open),
                stop_loss=float(item.sl), take_profit=float(item.tp),
                open_time=server_time_from_epoch(item.time, self._clock),
                profit=float(item.profit), magic=int(item.magic), comment=str(item.comment),
                # Reconstructed by the engine from the journal; the broker does not store
                # them, and `initial_stop = sl` is the fail-safe fallback (§8.6 step 3).
                initial_stop=float(item.sl), initial_volume=float(item.volume),
                partial_taken=False,
            ))
        return out

    def closed_deals(self, since: datetime, magic: int) -> list[ClosedTrade]:
        """Return the bot's completed round trips since ``since``.

        Reconstructed from ``history_deals_get`` by pairing each closing deal with the
        position it belongs to. Deals are the authority: a position that vanished while
        the bot was down closed here, and this is how the governor finds out (§8.6 step 4).
        """
        api = require_mt5()
        deals = api.history_deals_get(since, self._clock.now() + timedelta(minutes=1))
        if deals is None:
            return []
        by_position: dict[int, list[Any]] = {}
        for deal in deals:
            if deal.magic != magic:
                continue
            by_position.setdefault(int(deal.position_id), []).append(deal)

        out: list[ClosedTrade] = []
        for position_id, legs in by_position.items():
            legs.sort(key=lambda d: d.time)
            closes = [d for d in legs if d.entry == _DEAL_ENTRY_OUT]
            opens = [d for d in legs if d.entry != _DEAL_ENTRY_OUT]
            if not closes or not opens:
                continue
            first, last = opens[0], closes[-1]
            volume = sum(float(d.volume) for d in closes)
            gross = sum(float(d.profit) for d in closes)
            commission = sum(float(d.commission) for d in legs)
            swap = sum(float(d.swap) for d in legs)
            side = Side.BUY if first.type == api.DEAL_TYPE_BUY else Side.SELL
            out.append(ClosedTrade(
                ticket=position_id, symbol=str(first.symbol), side=side, volume=volume,
                entry_price=float(first.price), exit_price=float(last.price),
                entry_time=server_time_from_epoch(first.time, self._clock),
                exit_time=server_time_from_epoch(last.time, self._clock),
                initial_stop=0.0, gross_pnl=gross, commission=commission, swap=swap,
                net_pnl=gross + commission + swap, r_multiple=0.0, mae_r=0.0, mfe_r=0.0,
                exit_reason=_exit_reason(str(last.comment)), magic=magic,
            ))
        return sorted(out, key=lambda t: t.exit_time)

    # ------------------------------------------------------------------ internals

    def _candidates(self, spec: SymbolSpec) -> list[int]:
        """Return the filling candidates, cached winner first."""
        candidates = filling_candidates(spec)
        cached = self._filling.get(spec.name)
        if cached is not None and cached in candidates:
            return [cached, *[c for c in candidates if c != cached]]
        return candidates

    def _comment(self, token: str) -> str:
        """Build the order comment: ``prefix|token``, ASCII, at most 31 characters."""
        prefix = self._cfg.execution.order_comment_prefix
        comment = f"{prefix}|{token[:8]}"
        return comment.encode("ascii", "ignore").decode("ascii")[:_COMMENT_MAX]

    def _validated_stop(self, order: SizedOrder, price: float, spec: SymbolSpec,
                        spread_points: int) -> float:
        """Re-validate the stop against ``stops_level`` at the current price."""
        minimum = spec.min_stop_distance(spread_points)
        stop = order.stop_price
        stop = min(stop, price - minimum) if order.side is Side.BUY else max(stop,
                                                                            price + minimum)
        return spec.round_stop_away(stop, price)

    def _already_delivered(self, order: SizedOrder, symbol: str) -> bool:
        """Return whether this approval already reached the market (§9.5).

        **Comment matching is best-effort, not authoritative.** Brokers truncate or rewrite
        the comment field, and MT5 replaces a stopped-out deal's comment with
        ``[sl 1.08320]``. The authoritative check is ``history_orders_get`` over the last
        60 seconds, filtered in Python on magic and symbol: any order for this symbol and
        magic set up at or after the approval was created means the send already happened.
        """
        api = require_mt5()
        now = self._clock.now()
        window_start = now - timedelta(seconds=_IDEMPOTENCY_WINDOW_S)
        orders = api.history_orders_get(window_start, now + timedelta(seconds=1))
        magic = self._cfg.execution.magic
        for item in orders or []:
            if item.magic != magic or item.symbol != symbol:
                continue
            comment = str(getattr(item, "comment", ""))
            if order.approval_id[:8] not in comment:
                # Log loudly: a comment that does not round-trip silently disables the
                # secondary idempotency check, and you want to know before it matters.
                log.warning("order_comment_altered", symbol=symbol,
                            approval_id=order.approval_id[:8], broker_comment=comment)
            return True
        for item in api.positions_get(symbol=symbol) or []:
            if item.magic == magic and order.approval_id[:8] in str(item.comment):
                return True
        return False

    def _poll_for_position(self, order: SizedOrder, symbol: str) -> OrderResult | None:
        """Poll for the position after a 10008 ``PLACED``."""
        api = require_mt5()
        deadline = _POLL_SECONDS
        waited = 0.0
        while waited < deadline:
            for item in api.positions_get(symbol=symbol) or []:
                if (item.magic == self._cfg.execution.magic
                        and order.approval_id[:8] in str(item.comment)):
                    return OrderResult(ok=True, retcode=TRADE_RETCODE_DONE,
                                       ticket=int(item.ticket), filled_volume=float(item.volume),
                                       filled_price=float(item.price_open), slippage_points=0.0,
                                       comment="confirmed by positions_get after PLACED",
                                       request_id=order.approval_id)
            self._sleep(_POLL_INTERVAL_S)
            waited += _POLL_INTERVAL_S
        return None

    def _confirm(self, order: SizedOrder, result: Any, intended: float,
                 spec: SymbolSpec) -> OrderResult:
        """Confirm a DONE send actually produced a position, and measure slippage.

        If ``order_send`` timed out but the order really filled, this is how you find out
        (§9.2 step 8).
        """
        api = require_mt5()
        ticket = int(getattr(result, "order", 0)) or None
        filled_price = float(getattr(result, "price", 0.0)) or intended
        found = api.positions_get(symbol=spec.name) or []
        for item in found:
            if item.magic == self._cfg.execution.magic and order.approval_id[:8] in str(
                    item.comment):
                ticket = int(item.ticket)
                filled_price = float(item.price_open)
                break
        slippage = abs(filled_price - intended) / spec.point if spec.point else 0.0
        return OrderResult(ok=True, retcode=TRADE_RETCODE_DONE, ticket=ticket,
                           filled_volume=float(getattr(result, "volume", order.volume)),
                           filled_price=filled_price, slippage_points=slippage,
                           comment=str(getattr(result, "comment", "")),
                           request_id=order.approval_id)

    def _fail(self, order: SizedOrder, detail: str,
              retcode: int = TRADE_RETCODE_REJECT) -> OrderResult:
        """Build a failed :class:`OrderResult` carrying the reason."""
        return OrderResult(ok=False, retcode=retcode, ticket=None, filled_volume=0.0,
                           filled_price=0.0, slippage_points=0.0,
                           comment=f"{order.symbol}: {detail}", request_id=order.approval_id)


def _exit_reason(comment: str) -> str:
    """Map an MT5 deal comment to one of the ``ClosedTrade.exit_reason`` values."""
    lowered = comment.lower()
    if "[sl" in lowered or lowered.startswith("sl"):
        return "stop"
    if "[tp" in lowered:
        return "tp1"
    return "manual"
