"""The Broker port (§9.1).

:class:`~fxbot.execution.mt5_broker.MT5Broker` and
:class:`~fxbot.execution.paper_broker.PaperBroker` both implement it. The engine holds a
``Broker``, never ``MetaTrader5``.

``OrderResult``, ``Approval`` and ``ClosedTrade`` live in ``core/models.py`` (§4), not
here: ``risk/governor.py`` reads ``result.retcode`` in ``record_fill()`` and may not
import ``execution/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from fxbot.core.models import ClosedTrade, OrderResult, Position, SizedOrder


class Broker(Protocol):
    """Everything the engine is allowed to ask a broker to do.

    ``positions(magic)`` and ``closed_deals(..., magic)`` are the **bot's** API and take a
    magic number; ``MT5Broker`` implements them by calling ``mt5.positions_get()`` /
    ``mt5.history_deals_get()`` and filtering in Python (§8.6), because the MT5 functions
    themselves have no magic parameter.
    """

    def open(self, order: SizedOrder) -> OrderResult:
        """Send a market order. Refuses any order without an ``approval_id`` (§0.4)."""
        ...

    def close(self, ticket: int, volume: float | None = None,
              reason: str = "manual") -> OrderResult:
        """Close a position, wholly or partially.

        ``reason`` extends §9.1's signature with a defaulted argument -- every call written
        against the original two-parameter form still type-checks. It exists because
        ``ClosedTrade.exit_reason`` is required by §4 and only the caller knows whether a
        close was ``tp1``, ``bias_flip`` or an operator flatten.
        """
        ...

    def modify_stop(self, ticket: int, stop: float, take_profit: float | None) -> OrderResult:
        """Move a position's server-side stop."""
        ...

    def positions(self, magic: int) -> list[Position]:
        """Return the bot's open positions."""
        ...

    def closed_deals(self, since: datetime, magic: int) -> list[ClosedTrade]:
        """Return the bot's completed round trips since ``since``."""
        ...
