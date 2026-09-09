"""Position sizing (§8.2). Pure, and the most test-covered function in the repo.

A bot that trades less than intended costs opportunity; a bot that risks more than
intended costs the account (§18). Every rounding decision here is therefore biased
downward, without exception.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from fxbot.core.enums import RejectReason
from fxbot.core.errors import SizingError
from fxbot.core.models import SymbolSpec


@dataclass(frozen=True, slots=True)
class SizingResult:
    """The outcome of one sizing calculation."""

    volume: float
    """0.0 means "do not trade"."""
    risk_amount: float
    risk_pct: float
    reason: RejectReason
    detail: str


def position_size(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    spec: SymbolSpec,
    commission_per_lot_round_turn: float,
    size_multiplier: float = 1.0,
) -> SizingResult:
    """Return the volume to trade, or 0.0 with a reason.

    ``entry_price`` is the **expected fill price, not the signal bar's close**:
    ``tick.ask`` (BUY) / ``tick.bid`` (SELL) in live, and the next bar's open adjusted for
    the spread in backtest. Sizing off ``Signal.entry_ref`` while filling one bar later
    understates the stop distance by the gap plus the spread, so realised risk quietly
    exceeds the budget -- the one failure §18 says outranks everything.

    Args:
        equity: Account equity, never balance (§17.8).
        risk_pct: Percent of equity to risk on this trade, e.g. ``0.5``.
        entry_price: Expected fill price.
        stop_price: The protective stop.
        spec: The symbol specification, straight from ``mt5.symbol_info()``.
        commission_per_lot_round_turn: Round-turn commission per lot, account currency.
        size_multiplier: Governor size multiplier (``0.5`` in ``REDUCED``).

    Returns:
        A :class:`SizingResult`. ``volume`` is a exact multiple of ``spec.volume_step``
        and ``risk_amount`` never exceeds the risk budget.

    Raises:
        SizingError: If the symbol's tick value or tick size is unusable. The bot never
            guesses a pip value -- ``$10/pip`` is wrong for most symbol/account currency
            combinations, and a wrong guess mis-sizes every trade in that symbol.
    """
    if equity <= 0.0:
        return SizingResult(0.0, 0.0, 0.0, RejectReason.SIZE_BELOW_MIN,
                            f"non-positive equity {equity}")
    if size_multiplier <= 0.0:
        return SizingResult(0.0, 0.0, 0.0, RejectReason.SIZE_BELOW_MIN,
                            f"non-positive size multiplier {size_multiplier}")

    # 1. Stop distance.
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0.0 or not math.isfinite(stop_distance):
        return SizingResult(0.0, 0.0, 0.0, RejectReason.SIZE_BELOW_MIN, "zero stop distance")

    # 2. Value of one full price unit of movement, per 1.00 lot, in account currency.
    if spec.tick_size <= 0.0:
        raise SizingError(f"{spec.name}: non-positive tick_size {spec.tick_size}")
    value_per_price_unit_per_lot = spec.tick_value / spec.tick_size
    if value_per_price_unit_per_lot <= 0.0 or not math.isfinite(value_per_price_unit_per_lot):
        raise SizingError(
            f"{spec.name}: unusable tick_value={spec.tick_value} / tick_size={spec.tick_size}. "
            "Never guess a pip value."
        )

    # 3. Risk budget.
    risk_budget = equity * (risk_pct / 100.0) * size_multiplier
    if risk_budget <= 0.0:
        return SizingResult(0.0, 0.0, 0.0, RejectReason.SIZE_BELOW_MIN,
                            f"non-positive risk budget {risk_budget}")

    # 4. Cost-inclusive denominator: the stop loss AND the round-turn commission are both
    #    money you lose on a losing trade. Excluding commission systematically oversizes.
    cost_per_lot = stop_distance * value_per_price_unit_per_lot + commission_per_lot_round_turn
    if cost_per_lot <= 0.0:
        raise SizingError(f"{spec.name}: non-positive cost per lot {cost_per_lot}")
    raw_volume = risk_budget / cost_per_lot

    # 5. Round DOWN to the volume step. Always down; rounding up breaks the risk limit.
    volume = spec.floor_volume(raw_volume)

    # 6. Below the broker minimum is a refusal, never a round-up. An account too small for
    #    the stop must not trade (§17.7).
    if volume < spec.volume_min:
        return SizingResult(
            0.0, 0.0, 0.0, RejectReason.SIZE_BELOW_MIN,
            f"raw={raw_volume:.4f} < min={spec.volume_min}",
        )
    volume = min(volume, spec.volume_max)

    # 7. Realised risk at this volume.
    risk_amount = volume * cost_per_lot
    return SizingResult(volume, risk_amount, 100.0 * risk_amount / equity,
                        RejectReason.NONE, "")


def position_risk_amount(
    volume: float,
    entry_price: float,
    stop_price: float,
    spec: SymbolSpec,
    commission_per_lot_round_turn: float,
) -> float:
    """Return the account-currency risk of an existing position at its current stop.

    Uses the identical commission-inclusive definition :func:`position_size` uses, so
    ``risk/exposure.py`` and ``risk/sizing.py`` cannot drift into disagreeing about what
    "0.5% risk" means (§8.4).

    Args:
        volume: Position volume in lots.
        entry_price: Entry price.
        stop_price: The stop currently protecting the position.
        spec: The symbol specification.
        commission_per_lot_round_turn: Round-turn commission per lot.

    Returns:
        The money at risk, in account currency. Zero when no stop is set: the caller
        treats that as a reconciliation problem, not as a free position.
    """
    if stop_price <= 0.0:
        return 0.0
    distance = abs(entry_price - stop_price)
    return volume * (distance * spec.value_per_price_unit_per_lot
                     + commission_per_lot_round_turn)
