"""Retcode classification tests (§9.4). Classify, then act. Never blind-retry."""

from __future__ import annotations

import pytest

from fxbot.execution.retry import (
    TRADE_RETCODE_CLIENT_DISABLES_AT,
    TRADE_RETCODE_CONNECTION,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_INVALID,
    TRADE_RETCODE_INVALID_FILL,
    TRADE_RETCODE_INVALID_STOPS,
    TRADE_RETCODE_INVALID_VOLUME,
    TRADE_RETCODE_MARKET_CLOSED,
    TRADE_RETCODE_NO_MONEY,
    TRADE_RETCODE_PLACED,
    TRADE_RETCODE_REJECT,
    TRADE_RETCODE_REQUOTE,
    Action,
    backoff_delays,
    classify,
)


@pytest.mark.parametrize(
    ("retcode", "action"),
    [
        (TRADE_RETCODE_DONE, Action.SUCCESS),
        (TRADE_RETCODE_PLACED, Action.POLL),
        (TRADE_RETCODE_REQUOTE, Action.RETRY_REPRICE),
        (TRADE_RETCODE_REJECT, Action.ABANDON),
        (TRADE_RETCODE_INVALID, Action.ABANDON),
        (TRADE_RETCODE_INVALID_VOLUME, Action.HALT),
        (TRADE_RETCODE_INVALID_STOPS, Action.RETRY_RESTOP),
        (TRADE_RETCODE_MARKET_CLOSED, Action.SKIP_SYMBOL),
        (TRADE_RETCODE_NO_MONEY, Action.HALT),
        (TRADE_RETCODE_CLIENT_DISABLES_AT, Action.HALT),
        (TRADE_RETCODE_INVALID_FILL, Action.RETRY_NEXT_FILLING),
        (TRADE_RETCODE_CONNECTION, Action.RECONNECT),
    ],
)
def test_the_whole_table_from_the_spec(retcode: int, action: Action) -> None:
    """Every row of §9.4's table, asserted."""
    assert classify(retcode).action is action


def test_a_sizing_disagreement_halts_rather_than_retrying() -> None:
    """10014 means ``sizing.py`` disagrees with the broker; retrying sends it again."""
    decision = classify(TRADE_RETCODE_INVALID_VOLUME)
    assert decision.action is Action.HALT
    assert decision.retryable is False
    assert "sizing.py" in decision.detail


def test_algo_trading_off_is_named_as_a_vps_configuration_error() -> None:
    """10027 is the Algo Trading button. The message has to say so at 3am (§13.3)."""
    assert "Algo Trading" in classify(TRADE_RETCODE_CLIENT_DISABLES_AT).detail


def test_market_closed_is_not_alerted() -> None:
    """A closed session is not an error and must not page anyone."""
    decision = classify(TRADE_RETCODE_MARKET_CLOSED)
    assert decision.alert is False
    assert decision.retryable is False


def test_an_unknown_retcode_is_abandoned_and_alerted() -> None:
    """Uncertainty stops trading; it is never retried blindly (§0.7)."""
    decision = classify(99999)
    assert decision.action is Action.ABANDON
    assert decision.retryable is False
    assert decision.alert is True


def test_backoff_is_exponential() -> None:
    """1.5s, 3s, 6s -- and an empty schedule when retries are disabled."""
    assert backoff_delays(3, 1.5) == [1.5, 3.0, 6.0]
    assert backoff_delays(0, 1.5) == []
