"""Enumerations shared by every layer (§4).

All are :class:`~enum.StrEnum` so they serialise to the journal and the JSON logs as
their own names with no adapter code.
"""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    """Direction of a position or order."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        """The side that closes a position of this side."""
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """``+1`` for BUY, ``-1`` for SELL."""
        return 1 if self is Side.BUY else -1


class Bias(StrEnum):
    """Higher-timeframe directional permission (§7.3 step 1)."""

    LONG_ONLY = "LONG_ONLY"
    SHORT_ONLY = "SHORT_ONLY"
    NEUTRAL = "NEUTRAL"


class Regime(StrEnum):
    """H1 market regime classification (§7.3 step 2)."""

    TRENDING = "TRENDING"
    RANGING = "RANGING"
    EXTREME = "EXTREME"


class IntentKind(StrEnum):
    """The kinds of action position management may request (§7.4)."""

    OPEN = "OPEN"
    CLOSE = "CLOSE"
    CLOSE_PARTIAL = "CLOSE_PARTIAL"
    MODIFY_STOP = "MODIFY_STOP"
    NONE = "NONE"


class RiskStatus(StrEnum):
    """Governor state (§8.5)."""

    NORMAL = "NORMAL"
    REDUCED = "REDUCED"
    DAILY_LOCKOUT = "DAILY_LOCKOUT"
    HALTED = "HALTED"


class RejectReason(StrEnum):
    """Every refusal in the system is exactly one of these, logged verbatim (§4).

    The reject-reason histogram built from these values is the primary debugging tool
    (§10.3): "why did it not trade for three weeks?" is answered by one query.
    """

    NONE = "NONE"
    # Strategy
    REGIME = "REGIME"
    BIAS = "BIAS"
    NO_TRIGGER = "NO_TRIGGER"
    CONFIRMATION = "CONFIRMATION"
    SESSION_CLOSED = "SESSION_CLOSED"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    STALE_DATA = "STALE_DATA"
    # Governor
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    # Exposure
    MAX_POSITIONS = "MAX_POSITIONS"
    SYMBOL_ALREADY_OPEN = "SYMBOL_ALREADY_OPEN"
    CLUSTER_LIMIT = "CLUSTER_LIMIT"
    TOTAL_RISK_CAP = "TOTAL_RISK_CAP"
    # Sizing / execution
    SIZE_BELOW_MIN = "SIZE_BELOW_MIN"
    MARGIN_INSUFFICIENT = "MARGIN_INSUFFICIENT"
    KILL_SWITCH = "KILL_SWITCH"
    BROKER_ERROR = "BROKER_ERROR"
