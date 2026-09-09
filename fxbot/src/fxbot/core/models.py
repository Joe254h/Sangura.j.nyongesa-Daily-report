"""Frozen data contracts crossing every layer boundary (§4).

All models are ``@dataclass(frozen=True, slots=True)``: no mutable shared state passes
between layers. Prices are floats in symbol quote units; volumes are floats in lots.

**Time convention.** MT5 rate and tick timestamps are in *broker server time*, not UTC.
Every :class:`~datetime.datetime` crossing a layer boundary is therefore timezone-aware
in broker server time, carrying the resolved fixed offset as its ``tzinfo``. UTC appears
only inside ``ops/`` log records, converted explicitly at the point of writing.

**Ambiguity resolved (§18).** :class:`StrategyContext` carries ``StrategyParams`` and
``SessionParams``, which §5 places in ``config/schema.py`` -- and ``config/`` imports
``core``. A runtime import here would be a cycle, so both names are imported under
``typing.TYPE_CHECKING`` only and the annotations stay strings. ``tests/test_layering.py``
ignores ``TYPE_CHECKING``-guarded imports for exactly this reason; no runtime dependency
from ``core`` to ``config`` exists.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    import pandas as pd

    from fxbot.config.schema import SessionParams, StrategyParams

from fxbot.core.enums import Bias, IntentKind, Regime, RejectReason, RiskStatus, Side


@dataclass(frozen=True, slots=True)
class Bar:
    """One closed OHLCV bar in broker server time."""

    time: datetime
    """Broker server time (tz-aware), bar OPEN time."""
    open: float
    high: float
    low: float
    close: float
    volume: int
    """Tick volume."""
    spread: int
    """Points, as reported by MT5."""


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Everything about a symbol needed to size and place an order.

    Populated ONLY from ``mt5.symbol_info()``. Never hand-written except in fixtures.
    """

    name: str
    digits: int
    point: float
    """e.g. 0.00001."""
    tick_size: float
    """``trade_tick_size``."""
    tick_value: float
    """``trade_tick_value_loss`` -- the loss-side tick price in account currency per 1.00
    lot. NOT ``trade_tick_value`` (which equals ``_PROFIT``). Sizing is a loss
    calculation; reversing this mis-sizes exactly the crosses §6.2 warns about."""
    tick_value_profit: float
    """``trade_tick_value_profit`` -- P/L reporting only."""
    contract_size: float
    swap_long: float
    swap_short: float
    swap_mode: int
    trade_exemode: int
    """``SYMBOL_TRADE_EXECUTION_*`` -- decides filling modes (§9.3)."""
    currency_base: str
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int
    """Points; minimum SL/TP distance from price."""
    freeze_level: int
    """Points."""
    filling_modes: int
    """Bitmask from ``symbol_info().filling_mode``."""
    currency_profit: str
    currency_margin: str

    @property
    def value_per_price_unit_per_lot(self) -> float:
        """Account-currency value of one full price unit of movement, per 1.00 lot."""
        return self.tick_value / self.tick_size

    def floor_volume(self, volume: float) -> float:
        """Round ``volume`` **down** to a multiple of :attr:`volume_step`.

        The ratio is rounded to nine decimal places before flooring, which kills float
        dust without ever crossing a step boundary upward. ``floor(x + 1e-9)`` would cross
        it, and the property test in ``tests/test_sizing.py`` catches that (§8.2 step 5).

        Args:
            volume: The unrounded volume in lots.

        Returns:
            The largest multiple of :attr:`volume_step` not exceeding ``volume``.

        Raises:
            ValueError: If :attr:`volume_step` is not positive.
        """
        if self.volume_step <= 0.0:
            raise ValueError(f"{self.name}: volume_step must be positive, got {self.volume_step}")
        steps = math.floor(round(volume / self.volume_step, 9))
        return round(steps * self.volume_step, 8)

    def round_stop_away(self, stop: float, reference: float) -> float:
        """Round ``stop`` to :attr:`digits`, always **away from** ``reference``.

        Rounding a stop toward the entry silently increases risk on every trade, which is
        a systematic understatement of the loss sizing was built on (§7.3 step 8).

        Args:
            stop: The unrounded stop price.
            reference: The price the stop is measured from.

        Returns:
            The rounded stop, never closer to ``reference`` than the input.
        """
        scale = 10.0**self.digits
        if stop < reference:
            return math.floor(stop * scale) / scale
        if stop > reference:
            return math.ceil(stop * scale) / scale
        return round(stop, self.digits)

    def min_stop_distance(self, spread_points: int) -> float:
        """Return the broker's minimum stop distance from a reference price, in price units.

        ``(stops_level + spread) * point``: the spread term is there because the stop is
        checked against the *other* side of the quote, so a stop that clears
        ``stops_level`` against the bid can still be rejected against the ask.

        Args:
            spread_points: Current spread in points.

        Returns:
            The minimum distance in price units.
        """
        return (self.stops_level + max(spread_points, 0)) * self.point


@dataclass(frozen=True, slots=True)
class AccountState:
    """A snapshot of the trading account, in account currency."""

    equity: float
    balance: float
    margin: float
    margin_free: float
    currency: str
    leverage: int
    server_time: datetime


@dataclass(frozen=True, slots=True)
class Position:
    """An open position, as the bot understands it.

    The last three fields are bot-managed and persisted in the journal: the broker does
    not store them. ``initial_stop`` and ``initial_volume`` are frozen at open and never
    updated, including after a partial close -- R is always measured against the original
    stop and entry (§7.4).
    """

    ticket: int
    symbol: str
    side: Side
    volume: float
    entry_price: float
    stop_loss: float
    """0.0 means none -- always set one."""
    take_profit: float
    """Always 0.0 in v1; see §7.4 -- exits are partial + trail."""
    open_time: datetime
    profit: float
    """Floating P/L, account currency."""
    magic: int
    comment: str
    initial_stop: float
    """For R computation; NEVER updated after open."""
    initial_volume: float
    partial_taken: bool


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Result of :func:`fxbot.data.quality.check` for one symbol (§6.4)."""

    ok: bool
    reason: RejectReason
    """``NONE`` when ``ok``."""
    detail: str
    bars: int
    last_close: datetime
    gap_count: int
    fatal_sanity: bool
    """Bad tick / impossible OHLC -- also suppresses ``MODIFY_STOP`` (§6.4)."""


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """The ONLY input to :func:`generate_signal`.

    If a decision needs data it must appear here -- no globals, no lookups, no clock
    reads.
    """

    symbol: str
    now: datetime
    """Server time of the just-closed bar's CLOSE."""
    h1: pd.DataFrame
    """Closed H1 bars, ascending, ``>= warmup_bars`` rows."""
    d1: pd.DataFrame
    """Closed D1 bars, ascending."""
    spec: SymbolSpec
    current_spread_points: int
    open_position: Position | None
    params: StrategyParams
    session: SessionParams
    """Frozen -- §7.3 step 3 is evaluated INSIDE the strategy, because no clock object
    may cross the purity boundary (§0.2)."""
    max_spread_points: int
    """Resolved for THIS symbol by the caller -- §7.3 step 4."""
    commission_per_lot_round_turn: float
    """Round-turn commission per lot, in account currency.

    **Ambiguity resolved (§18).** §7.4 rule 3 requires the breakeven stop to sit at
    ``entry +/- cost_buffer`` where the buffer "covers spread + commission converted to
    price units", but §4's context carries no cost figure and ``strategy/`` may not read
    config. Three readings were possible: approximate the commission as a multiple of the
    spread (arbitrary), move the number into ``StrategyParams`` (it is a broker fact, not
    a strategy choice), or carry it on the context. The third is chosen because this
    dataclass's own contract says it: "if a decision needs data, it must appear here".
    The caller resolves it from ``cfg.costs.commission_per_lot_round_turn``."""
    quality: QualityReport


@dataclass(frozen=True, slots=True)
class Signal:
    """The strategy's verdict for one symbol on one closed bar."""

    side: Side | None
    regime: Regime
    bias: Bias
    entry_ref: float
    """Reference price (close of the signal bar). NOT the price to size from (§8.2)."""
    stop_price: float
    atr: float
    adx: float
    reason: RejectReason
    """``NONE`` when ``side`` is not None."""
    diagnostics: Mapping[str, float]
    """Every indicator value used, for the journal."""


@dataclass(frozen=True, slots=True)
class Intent:
    """Exactly one requested action, produced by :func:`manage_position` (§7.4)."""

    kind: IntentKind
    symbol: str
    side: Side | None = None
    stop_price: float | None = None
    take_profit: float | None = None
    close_fraction: float | None = None
    """For ``CLOSE_PARTIAL``, ``0 < f <= 1``."""
    ticket: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """A completed round trip, as recorded in the journal."""

    ticket: int
    symbol: str
    side: Side
    volume: float
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    initial_stop: float
    gross_pnl: float
    commission: float
    swap: float
    net_pnl: float
    r_multiple: float
    mae_r: float
    mfe_r: float
    exit_reason: str
    """``"stop" | "tp1" | "trail" | "bias_flip" | "manual"``."""
    magic: int


@dataclass(frozen=True, slots=True)
class SizedOrder:
    """An order the governor has approved. Execution refuses orders without one."""

    symbol: str
    side: Side
    volume: float
    stop_price: float
    take_profit: float | None
    risk_amount: float
    """Account currency actually at risk."""
    risk_pct: float
    approval_id: str
    """UUID from :class:`~fxbot.risk.governor.RiskGovernor`."""


@dataclass(frozen=True, slots=True)
class Approval:
    """The result of :meth:`~fxbot.risk.governor.RiskGovernor.approve`."""

    ok: bool
    order: SizedOrder | None
    reason: RejectReason
    approval_id: str
    """UUID; echoed into the order comment (§9.5)."""
    risk_status: RiskStatus
    created_at: datetime
    detail: str


@dataclass(frozen=True, slots=True)
class OrderResult:
    """The outcome of one broker round trip."""

    ok: bool
    retcode: int
    ticket: int | None
    filled_volume: float
    filled_price: float
    slippage_points: float
    comment: str
    request_id: str


class JournalSink(Protocol):
    """Write port for the journal.

    Declared in ``core/`` so ``risk/`` can write to the journal without importing
    ``runtime/`` (which would invert the dependency arrows, §2.1).
    ``runtime/journal.py`` implements it.
    """

    def record_decision(self, signal: Signal, ctx_meta: Mapping[str, object]) -> None: ...
    def record_approval(self, approval: Approval) -> None: ...
    def record_order(self, order: SizedOrder, result: OrderResult) -> None: ...
    def record_trade(self, trade: ClosedTrade) -> None: ...
    def record_risk_event(self, before: RiskStatus, after: RiskStatus, detail: str) -> None: ...
