"""Filling-mode negotiation tests (§9.3).

Retcode 10030 is the single most common first-day failure with MT5 Python.
``ORDER_FILLING_RETURN`` is not a fallback, and the tests say so.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from fxbot.core.errors import SymbolResolutionError
from fxbot.execution.filling import (
    ORDER_FILLING_FOK,
    ORDER_FILLING_IOC,
    SYMBOL_FILLING_BOC,
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    FillingCache,
    filling_candidates,
    negotiate_filling_mode,
)


def test_ioc_is_preferred_for_market_orders(eurusd) -> None:
    """Preference for market orders is IOC, then FOK."""
    both = replace(eurusd, filling_modes=SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC)
    assert filling_candidates(both) == [ORDER_FILLING_IOC, ORDER_FILLING_FOK]
    assert negotiate_filling_mode(both) == ORDER_FILLING_IOC


def test_each_bitmask_value_maps_to_the_right_candidate(eurusd) -> None:
    """One bit set gives exactly one candidate."""
    ioc_only = replace(eurusd, filling_modes=SYMBOL_FILLING_IOC)
    fok_only = replace(eurusd, filling_modes=SYMBOL_FILLING_FOK)
    assert filling_candidates(ioc_only) == [ORDER_FILLING_IOC]
    assert filling_candidates(fok_only) == [ORDER_FILLING_FOK]
    assert negotiate_filling_mode(fok_only) == ORDER_FILLING_FOK


def test_book_or_cancel_alone_is_refused(eurusd) -> None:
    """BOC is passive/limit only and is never used for a market order."""
    boc = replace(eurusd, filling_modes=SYMBOL_FILLING_BOC)
    with pytest.raises(SymbolResolutionError, match="neither IOC nor FOK"):
        filling_candidates(boc)


def test_return_is_not_a_fallback_on_a_market_execution_book(eurusd) -> None:
    """"Fall back to RETURN" loops on 10030 forever on a Razor book; fail closed instead."""
    none_set = replace(eurusd, filling_modes=0, trade_exemode=2)
    with pytest.raises(SymbolResolutionError) as exc:
        negotiate_filling_mode(none_set)
    assert "RETURN is not a fallback" in str(exc.value)


def test_the_cache_is_keyed_by_server_and_survives_a_reload(tmp_path) -> None:
    """A demo cache must never be carried into live: different server, different key."""
    path = tmp_path / "filling_modes.json"
    demo = FillingCache(path, "Pepperstone-Demo")
    demo.remember("EURUSD", ORDER_FILLING_IOC)

    live = FillingCache(path, "Pepperstone-Live")
    assert live.get("EURUSD") is None

    again = FillingCache(path, "Pepperstone-Demo")
    assert again.get("EURUSD") == ORDER_FILLING_IOC


def test_a_corrupt_cache_is_discarded_not_fatal(tmp_path) -> None:
    """The worst case is one 10030 and a retry, because candidates are always re-derived."""
    path = tmp_path / "filling_modes.json"
    path.write_text("not json at all", encoding="utf-8")
    cache = FillingCache(path, "Pepperstone-Demo")
    assert cache.get("EURUSD") is None
