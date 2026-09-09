"""Retcode classification and retry policy (§9.4).

Classify, then act. Never blind-retry: half of MT5's failure retcodes mean "your request
was wrong", and retrying a wrong request just sends it again.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_PLACED = 10008
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_INVALID = 10013
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_INVALID_PRICE = 10015
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_MARKET_CLOSED = 10018
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_PRICE_OFF = 10021
TRADE_RETCODE_CLIENT_DISABLES_AT = 10027
TRADE_RETCODE_INVALID_FILL = 10030
TRADE_RETCODE_CONNECTION = 10031


class Action(StrEnum):
    """What the executor should do about a retcode."""

    SUCCESS = "SUCCESS"
    POLL = "POLL"
    """Accepted but not filled: poll for the position for up to 5 seconds."""
    RETRY_REPRICE = "RETRY_REPRICE"
    """Re-fetch the tick, re-validate the stop, retry."""
    RETRY_NEXT_FILLING = "RETRY_NEXT_FILLING"
    """Advance to the next filling candidate and retry."""
    RETRY_RESTOP = "RETRY_RESTOP"
    """Recompute the stop from the current price, retry once, then abandon."""
    RECONNECT = "RECONNECT"
    ABANDON = "ABANDON"
    """No retry. Log the full request and alert."""
    SKIP_SYMBOL = "SKIP_SYMBOL"
    """Not an error: the market is closed for this symbol. No alert."""
    HALT = "HALT"


@dataclass(frozen=True, slots=True)
class Classification:
    """The decision for one retcode."""

    action: Action
    retryable: bool
    alert: bool
    detail: str


_TABLE: dict[int, Classification] = {
    TRADE_RETCODE_DONE: Classification(Action.SUCCESS, False, False, "filled"),
    TRADE_RETCODE_PLACED: Classification(Action.POLL, False, False,
                                         "accepted, not filled: poll for the position"),
    TRADE_RETCODE_REQUOTE: Classification(Action.RETRY_REPRICE, True, False, "requote"),
    TRADE_RETCODE_PRICE_OFF: Classification(Action.RETRY_REPRICE, True, False, "price off"),
    TRADE_RETCODE_INVALID_PRICE: Classification(Action.RETRY_REPRICE, True, True,
                                                "invalid price"),
    TRADE_RETCODE_REJECT: Classification(Action.ABANDON, False, True, "rejected by dealer"),
    TRADE_RETCODE_INVALID: Classification(Action.ABANDON, False, True, "malformed request"),
    TRADE_RETCODE_INVALID_VOLUME: Classification(
        Action.HALT, False, True,
        "invalid volume: sizing.py disagrees with the broker about volume_step/min/max"),
    TRADE_RETCODE_INVALID_STOPS: Classification(Action.RETRY_RESTOP, True, True,
                                                "stop inside stops_level"),
    TRADE_RETCODE_MARKET_CLOSED: Classification(Action.SKIP_SYMBOL, False, False,
                                                "market closed"),
    TRADE_RETCODE_NO_MONEY: Classification(Action.HALT, False, True, "insufficient funds"),
    TRADE_RETCODE_CLIENT_DISABLES_AT: Classification(
        Action.HALT, False, True,
        "Algo Trading is switched off in the terminal: a VPS configuration error (§13.3)"),
    TRADE_RETCODE_INVALID_FILL: Classification(Action.RETRY_NEXT_FILLING, True, False,
                                               "unsupported filling mode"),
    TRADE_RETCODE_CONNECTION: Classification(Action.RECONNECT, True, True,
                                             "no connection to the trade server"),
}


def classify(retcode: int) -> Classification:
    """Return what to do about ``retcode``.

    Args:
        retcode: The ``retcode`` field of an ``mt5.order_send`` result.

    Returns:
        Its :class:`Classification`. An unknown retcode is **abandoned and alerted**, never
        retried: an unrecognised failure is uncertainty, and uncertainty stops trading
        (§0.7).
    """
    known = _TABLE.get(retcode)
    if known is not None:
        return known
    return Classification(Action.ABANDON, False, True, f"unknown retcode {retcode}")


def backoff_delays(max_retries: int, base: float) -> list[float]:
    """Return the exponential backoff schedule for ``max_retries`` attempts.

    Args:
        max_retries: Number of retries after the first attempt.
        base: First delay in seconds; each subsequent delay doubles.

    Returns:
        The delays, e.g. ``[1.5, 3.0, 6.0]`` for ``max_retries=3, base=1.5``.
    """
    return [base * (2**i) for i in range(max(max_retries, 0))]
