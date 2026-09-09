"""The strategy port, plus the stop arithmetic the entry and exit rules share.

**Where the arithmetic lives (§3 note).** ``trend_donchian.py`` and ``manage.py`` both
round stops to the symbol's digits and both enforce the broker's ``stops_level``; sizing
has to agree with them about lot rounding to the last decimal. Two copies of that is
exactly the duplication §0.1 forbids -- and ``risk/sizing.py`` may not import
``strategy/`` (§2.1), so it cannot live here either. It sits on
:class:`~fxbot.core.models.SymbolSpec` in ``core/``, which every layer may import, and
the named wrappers below exist only so the call sites read as the spec's prose does.
No new file is introduced.
"""

from __future__ import annotations

import math
from typing import Protocol

from fxbot.core.enums import Side
from fxbot.core.models import Intent, Signal, StrategyContext, SymbolSpec


class Strategy(Protocol):
    """Bars in, intents out. The only interface both engines drive."""

    def generate_signal(self, ctx: StrategyContext) -> Signal:
        """Decide whether to open a position on the just-closed bar."""
        ...

    def manage_position(self, ctx: StrategyContext) -> Intent:
        """Decide what to do about the open position on the just-closed bar."""
        ...


def round_price(price: float, digits: int) -> float:
    """Round ``price`` to the symbol's quoted precision."""
    return round(price, digits)


def round_stop_away(stop: float, entry: float, digits: int) -> float:
    """Round ``stop`` away from ``entry`` at ``digits`` decimal places.

    Thin wrapper over :meth:`fxbot.core.models.SymbolSpec.round_stop_away` for call sites
    that hold digits rather than a spec.
    """
    scale = 10.0**digits
    if stop < entry:
        return math.floor(stop * scale) / scale
    if stop > entry:
        return math.ceil(stop * scale) / scale
    return round(stop, digits)


def min_stop_distance(spec: SymbolSpec, spread_points: int) -> float:
    """Return the broker's minimum stop distance in price units."""
    return spec.min_stop_distance(spread_points)


def enforce_stop_distance(
    stop: float, reference: float, side: Side, spec: SymbolSpec, spread_points: int
) -> float:
    """Widen ``stop`` if it sits inside the broker's minimum distance, then round it away.

    Args:
        stop: The intended stop price.
        reference: The price the broker measures the distance from.
        side: Side of the position the stop protects.
        spec: The symbol specification.
        spread_points: Current spread in points.

    Returns:
        A stop at least :meth:`SymbolSpec.min_stop_distance` away from ``reference``,
        rounded away from it.
    """
    minimum = spec.min_stop_distance(spread_points)
    stop = min(stop, reference - minimum) if side is Side.BUY else max(stop, reference + minimum)
    return spec.round_stop_away(stop, reference)


def floor_to_step(volume: float, step: float) -> float:
    """Round ``volume`` **down** to a multiple of ``step``.

    Kept as a free function for call sites that hold a bare step rather than a spec; the
    implementation of record is :meth:`fxbot.core.models.SymbolSpec.floor_volume`.
    """
    if step <= 0.0:
        raise ValueError(f"volume_step must be positive, got {step}")
    return round(math.floor(round(volume / step, 9)) * step, 8)


def partial_close_volume(volume: float, fraction: float, spec: SymbolSpec) -> float:
    """Return the volume a partial close would actually remove, after step rounding.

    The strategy and both brokers must agree on this number to the last decimal, or the
    "is the runner still above ``volume_min``?" test in §7.4 rule 2 answers one thing and
    the fill does another.

    Args:
        volume: Current position volume in lots.
        fraction: Requested fraction to close, ``0 < fraction <= 1``.
        spec: The symbol specification.

    Returns:
        The rounded close volume, possibly 0.0.
    """
    return spec.floor_volume(volume * fraction)
