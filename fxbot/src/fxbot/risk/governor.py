"""The risk governor and kill switch (§8.5).

The strategy decides *whether* and *which way*. This decides *how much*, and holds a veto
over everything. :meth:`RiskGovernor.approve` is the **only** path to
``Broker.place_order()``: there is no code path that sends an order without an approval
object (§0.4).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from fxbot.config.schema import AppConfig
from fxbot.core.clock import ServerClock
from fxbot.core.enums import RejectReason, RiskStatus, Side
from fxbot.core.errors import ClockError
from fxbot.core.models import (
    AccountState,
    Approval,
    ClosedTrade,
    JournalSink,
    OrderResult,
    Position,
    Signal,
    SizedOrder,
    StrategyContext,
    SymbolSpec,
)
from fxbot.risk.exposure import check_exposure
from fxbot.risk.sizing import position_size
from fxbot.risk.state import RiskState, RiskStateCorruptError, load_state, save_state

MarginCalculator = Callable[[SizedOrder], float]
"""Broker-supplied margin requirement for a candidate order, in account currency."""

_MT5_RETCODE_DONE = 10009
_CASH_MOVE_TOLERANCE = 0.01
"""Account-currency tolerance below which a balance move is rounding, not a deposit."""


class RiskGovernor:
    """Recomputes risk status every cycle and sizes every order that gets sent."""

    def __init__(
        self,
        cfg: AppConfig,
        state_path: Path,
        clock: ServerClock,
        journal: JournalSink,
    ) -> None:
        """Build a governor. Call :meth:`load` before the first cycle.

        Args:
            cfg: The resolved configuration.
            state_path: Where the risk state is persisted, e.g. ``state/risk_state.json``.
            clock: The broker clock -- every day boundary here is a broker day.
            journal: The journal sink, declared in ``core/`` so ``risk/`` never imports
                ``runtime/`` (§2.1).
        """
        self._cfg = cfg
        self._p = cfg.risk
        self._state_path = state_path
        self._clock = clock
        self._journal = journal
        self._state = RiskState()
        self._specs: dict[str, SymbolSpec] = {}
        self._margin_calc: MarginCalculator | None = None
        self._loaded = False

    # ---------------------------------------------------------------- wiring

    @property
    def state(self) -> RiskState:
        """The live risk state. Read-only by convention; mutate through the methods."""
        return self._state

    @property
    def status(self) -> RiskStatus:
        """The current risk status."""
        return self._state.status

    def set_symbol_specs(self, specs: Mapping[str, SymbolSpec]) -> None:
        """Provide the resolved symbol specifications exposure checks need."""
        self._specs = dict(specs)

    def set_margin_calculator(self, calc: MarginCalculator | None) -> None:
        """Inject the broker's margin calculation (§8.3).

        **Ambiguity resolved (§18).** §8.3 requires ``mt5.order_calc_margin`` before every
        approval, but ``risk/governor.py`` may not import ``MetaTrader5`` (§2.1) and §8.5
        fixes this class's constructor signature. The calculation is therefore injected as
        a callable, which the engine wires from the broker. When none is set -- the
        backtest, where there is no terminal to ask -- the margin gate is **skipped and
        said so** in ``Approval.detail``, rather than computed from leverage: §8.3 is
        explicit that the broker's number is the only authoritative one.
        """
        self._margin_calc = calc

    def size_multiplier(self) -> float:
        """Return the size multiplier implied by the current status."""
        return self._p.reduced_risk_multiplier if self._state.status is RiskStatus.REDUCED else 1.0

    # ---------------------------------------------------------------- persistence

    def load(self) -> None:
        """Read the persisted state.

        A missing file yields a fresh ``NORMAL`` state. A corrupt or unparseable file
        yields ``HALTED``: never start clean after a corrupt state file, because that is
        how a kill switch silently un-trips (§8.5).
        """
        try:
            loaded = load_state(self._state_path)
        except RiskStateCorruptError as exc:
            self._state = RiskState(status=RiskStatus.HALTED,
                                    halted_reason=f"corrupt risk state: {exc}")
            self._state.halted_at = self._safe_now()
            self._loaded = True
            self._journal.record_risk_event(RiskStatus.NORMAL, RiskStatus.HALTED, str(exc))
            self.save()
            return
        self._state = loaded if loaded is not None else RiskState()
        self._loaded = True

    def save(self) -> None:
        """Persist the state atomically. A torn write must not lose the kill switch."""
        self._state.last_update = self._safe_now()
        save_state(self._state_path, self._state)

    def _safe_now(self) -> datetime | None:
        """Return server time, or None before the clock has seen a broker timestamp.

        Boot order makes this real: :meth:`load` runs before the first account snapshot,
        so the clock may legitimately have nothing to report. Only :class:`ClockError` is
        caught -- anything else is a genuine fault and must propagate.
        """
        try:
            return self._clock.now()
        except ClockError:
            return None

    # ---------------------------------------------------------------- daily lifecycle

    def on_new_day(self, account: AccountState) -> None:
        """Roll the broker day (§8.5).

        Resets ``day_start_equity`` (from equity, not balance), ``realised_pnl_today``,
        ``trades_today`` **and** ``consecutive_losses``, then clears ``DAILY_LOCKOUT`` and
        ``REDUCED`` back to ``NORMAL``. Does NOT clear ``HALTED``. Does NOT reset the HWM.

        Resetting ``consecutive_losses`` here is mandatory, not cosmetic. If it survived
        the rollover, :meth:`refresh` would re-evaluate ``consecutive_losses >= 5`` on the
        very next cycle and re-lock; the counter can only fall on a winning trade, a
        winning trade needs an entry, and entries are blocked in ``DAILY_LOCKOUT``. The bot
        would brick itself permanently -- and because §0.5 persists the state, a restart
        would not clear it.
        """
        before = self._state.status
        self._state.trading_day = self._clock.trading_day(account.server_time)
        self._state.day_start_equity = account.equity
        self._state.realised_pnl_today = 0.0
        self._state.trades_today = 0
        self._state.consecutive_losses = 0
        self._state.deposits_today = 0.0
        self._state.last_balance = account.balance
        if account.equity > self._state.equity_hwm:
            self._state.equity_hwm = account.equity
        if self._state.status in (RiskStatus.DAILY_LOCKOUT, RiskStatus.REDUCED):
            self._state.status = RiskStatus.NORMAL
        if before is not self._state.status:
            self._journal.record_risk_event(before, self._state.status, "new broker day")
        self.save()

    # ---------------------------------------------------------------- status

    def refresh(self, account: AccountState,
                positions: Sequence[Position]) -> RiskStatus:  # noqa: ARG002
        """Recompute status from live equity. Called at the top of every cycle.

        Args:
            account: The current account snapshot.
            positions: Open positions, for the journal only -- the limits below are all
                equity-based, deliberately: measuring only realised P/L lets a bot sit in
                a 6% floating loss while believing it is flat (§8.5).

        Returns:
            The status after the recomputation.
        """
        if not self._loaded:
            raise RuntimeError("RiskGovernor.refresh() before load(): the kill switch is unread")

        before = self._state.status
        self._track_balance_changes(account)

        if self._state.day_start_equity <= 0.0:
            self._state.day_start_equity = account.equity
        if self._state.trading_day is None:
            self._state.trading_day = self._clock.trading_day(account.server_time)
        if account.equity > self._state.equity_hwm:
            self._state.equity_hwm = account.equity

        detail = ""
        status = self._state.status

        if status is not RiskStatus.HALTED:
            drawdown_from_hwm = (
                (self._state.equity_hwm - account.equity) / self._state.equity_hwm
                if self._state.equity_hwm > 0.0 else 0.0
            )
            drawdown_today = (
                (self._state.day_start_equity - account.equity) / self._state.day_start_equity
                if self._state.day_start_equity > 0.0 else 0.0
            )

            if drawdown_from_hwm >= self._p.max_drawdown_pct / 100.0:
                status = RiskStatus.HALTED
                detail = (f"equity {account.equity:.2f} is {100 * drawdown_from_hwm:.2f}% below "
                          f"HWM {self._state.equity_hwm:.2f}")
                self._state.halted_reason = detail
                self._state.halted_at = account.server_time
            elif drawdown_today >= self._p.daily_loss_limit_pct / 100.0:
                status = RiskStatus.DAILY_LOCKOUT
                detail = (f"day drawdown {100 * drawdown_today:.2f}% >= "
                          f"{self._p.daily_loss_limit_pct:.2f}% "
                          f"(equity {account.equity:.2f} vs day start "
                          f"{self._state.day_start_equity:.2f})")
            elif self._state.consecutive_losses >= self._p.max_consecutive_losses:
                status = RiskStatus.DAILY_LOCKOUT
                detail = f"{self._state.consecutive_losses} consecutive losses"
            elif self._state.consecutive_losses >= self._p.reduced_after_consecutive_losses:
                status = RiskStatus.REDUCED
                detail = f"{self._state.consecutive_losses} consecutive losses"
            elif status is RiskStatus.REDUCED:
                status = RiskStatus.NORMAL

        self._state.status = status
        if before is not status:
            self._journal.record_risk_event(before, status, detail)
        self.save()
        return status

    def _track_balance_changes(self, account: AccountState) -> None:
        """Bump the HWM by any deposit so a top-up does not read as a 30% drawdown (§8.5).

        A deposit is a balance move larger than the realised P/L that would explain it:
        :meth:`record_closed_trade` advances ``realised_pnl_today`` on every close, so
        comparing the two deltas isolates cash in and out from trading.
        """
        if self._state.last_balance <= 0.0:
            self._state.last_balance = account.balance
            self._state.last_realised_pnl = self._state.realised_pnl_today
            return

        balance_delta = account.balance - self._state.last_balance
        realised_delta = self._state.realised_pnl_today - self._state.last_realised_pnl
        cash_move = balance_delta - realised_delta

        # Only a *deposit* moves the HWM. A withdrawal must NOT lower it: the drawdown the
        # bot is being measured against really did happen, and lowering the HWM would let
        # a withdrawal reset the max-drawdown kill switch.
        if cash_move > _CASH_MOVE_TOLERANCE:
            self._state.equity_hwm += cash_move
            self._state.deposits_today += cash_move

        self._state.last_balance = account.balance
        self._state.last_realised_pnl = self._state.realised_pnl_today

    # ---------------------------------------------------------------- approvals

    def approve(
        self,
        signal: Signal,
        ctx: StrategyContext,
        account: AccountState,
        positions: Sequence[Position],
    ) -> Approval:
        """The ONLY way to get a :class:`~fxbot.core.models.SizedOrder`.

        Args:
            signal: The strategy's entry signal; must carry a side.
            ctx: The context the signal was produced from.
            account: The current account snapshot.
            positions: Every open position.

        Returns:
            An :class:`~fxbot.core.models.Approval`. ``order`` is None whenever ``ok`` is
            False, and ``reason`` names the rule that refused.
        """
        approval_id = str(uuid.uuid4())
        created_at = account.server_time

        def refuse(reason: RejectReason, detail: str = "") -> Approval:
            return Approval(ok=False, order=None, reason=reason, approval_id=approval_id,
                            risk_status=self._state.status, created_at=created_at, detail=detail)

        if signal.side is None:
            return refuse(signal.reason or RejectReason.NO_TRIGGER, "signal carries no side")
        if self._state.status is RiskStatus.HALTED:
            return refuse(RejectReason.KILL_SWITCH, self._state.halted_reason or "HALTED")
        if self._state.status is RiskStatus.DAILY_LOCKOUT:
            return refuse(RejectReason.DAILY_LOSS_LIMIT, "daily lockout in force")

        # Size from the expected fill price, not the signal bar's close (§8.2). The engine
        # re-sizes once more from a fresh tick immediately before sending (§9.2 step 4).
        entry_price = self._expected_fill(signal, ctx)
        sizing = position_size(
            equity=account.equity,
            risk_pct=self._p.risk_per_trade_pct,
            entry_price=entry_price,
            stop_price=signal.stop_price,
            spec=ctx.spec,
            commission_per_lot_round_turn=self._cfg.costs.commission_per_lot_round_turn,
            size_multiplier=self.size_multiplier(),
        )
        if sizing.volume <= 0.0:
            return refuse(sizing.reason, sizing.detail)

        order = SizedOrder(
            symbol=ctx.symbol,
            side=signal.side,
            volume=sizing.volume,
            stop_price=signal.stop_price,
            # No server-side take-profit in v1: the 1.5R exit is a partial close and the
            # runner is trailed, and a static TP can express neither (§7.4).
            take_profit=None,
            risk_amount=sizing.risk_amount,
            risk_pct=sizing.risk_pct,
            approval_id=approval_id,
        )

        specs = dict(self._specs)
        specs.setdefault(ctx.symbol, ctx.spec)
        exposure = check_exposure(
            order, positions, specs, account.equity, self._p,
            self._cfg.costs.commission_per_lot_round_turn,
        )
        if exposure is not RejectReason.NONE:
            return refuse(exposure, f"exposure rule {exposure}")

        detail = ""
        if self._margin_calc is not None:
            required = self._margin_calc(order)
            allowed = account.margin_free * self._p.max_margin_utilisation_pct / 100.0
            if required > allowed:
                return refuse(RejectReason.MARGIN_INSUFFICIENT,
                              f"margin {required:.2f} > {allowed:.2f} "
                              f"({self._p.max_margin_utilisation_pct}% of free margin)")
            detail = f"margin {required:.2f} <= {allowed:.2f}"
        else:
            detail = "margin check skipped: no broker margin calculator wired (§8.3)"

        return Approval(ok=True, order=order, reason=RejectReason.NONE, approval_id=approval_id,
                        risk_status=self._state.status, created_at=created_at, detail=detail)

    def _expected_fill(self, signal: Signal, ctx: StrategyContext) -> float:
        """Return the price the order is expected to fill at.

        The signal bar's close plus (BUY) or minus (SELL) the current spread: the live
        engine buys at the ask and sells at the bid, and the backtest fills at the next
        bar's open adjusted the same way. Sizing off ``entry_ref`` alone understates the
        stop distance by the spread and quietly overshoots the risk budget (§8.2).
        """
        spread_price = ctx.current_spread_points * ctx.spec.point
        return (signal.entry_ref + spread_price if signal.side is Side.BUY
                else signal.entry_ref)

    # ---------------------------------------------------------------- outcomes

    def record_fill(self, order: SizedOrder, result: OrderResult) -> None:
        """Record the outcome of a send. A rejected send does not count as a trade."""
        if result.ok and result.retcode == _MT5_RETCODE_DONE:
            self._state.trades_today += 1
            if order.approval_id and result.ticket is not None:
                self._state.open_tickets = sorted({*self._state.open_tickets, result.ticket})
        self._journal.record_order(order, result)
        self.save()

    def record_closed_trade(self, trade: ClosedTrade) -> None:
        """Update realised P/L and the loss streak, then re-evaluate status.

        Args:
            trade: The completed round trip.
        """
        before = self._state.status
        self._state.realised_pnl_today += trade.net_pnl
        if trade.net_pnl < 0.0:
            self._state.consecutive_losses += 1
        elif trade.net_pnl > 0.0:
            self._state.consecutive_losses = 0
        # A scratch trade (exactly 0.0) leaves the streak alone: it is neither a loss the
        # counter should punish nor a win that earns a reset.
        self._state.open_tickets = [t for t in self._state.open_tickets if t != trade.ticket]

        status = self._state.status
        detail = ""
        if status is not RiskStatus.HALTED:
            if self._state.consecutive_losses >= self._p.max_consecutive_losses:
                status = RiskStatus.DAILY_LOCKOUT
                detail = f"{self._state.consecutive_losses} consecutive losses"
            elif self._state.consecutive_losses >= self._p.reduced_after_consecutive_losses:
                status = RiskStatus.REDUCED
                detail = f"{self._state.consecutive_losses} consecutive losses"
            elif status is RiskStatus.REDUCED:
                status = RiskStatus.NORMAL
                detail = "loss streak cleared"
        self._state.status = status
        if before is not status:
            self._journal.record_risk_event(before, status, detail)
        self._journal.record_trade(trade)
        self.save()

    def halt(self, reason: str) -> None:
        """Drive the governor to ``HALTED``. Only a manual reset leaves this state."""
        before = self._state.status
        self._state.status = RiskStatus.HALTED
        self._state.halted_reason = reason
        self._state.halted_at = self._safe_now()
        if before is not RiskStatus.HALTED:
            self._journal.record_risk_event(before, RiskStatus.HALTED, reason)
        self.save()

    def manual_reset(self, operator: str) -> None:
        """Clear ``HALTED`` back to ``NORMAL``.

        Only callable from ``python -m fxbot.cli reset --operator <name>``. Logged,
        journalled as a risk event and alerted at CRITICAL.

        Args:
            operator: Who authorised the reset. Recorded verbatim.

        Raises:
            ValueError: If ``operator`` is empty -- an unattributed reset of a kill switch
                is not a reset, it is a hole in the audit trail.
        """
        if not operator.strip():
            raise ValueError("manual_reset requires an operator name")
        before = self._state.status
        self._state.status = RiskStatus.NORMAL
        self._state.halted_reason = ""
        self._state.halted_at = None
        self._state.consecutive_losses = 0
        self._journal.record_risk_event(before, RiskStatus.NORMAL, f"manual reset by {operator}")
        self.save()

    def blocks_entries(self) -> bool:
        """Return whether new entries are currently forbidden."""
        return self._state.status in (RiskStatus.DAILY_LOCKOUT, RiskStatus.HALTED)
