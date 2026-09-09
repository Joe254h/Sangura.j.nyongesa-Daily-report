"""Filling-mode negotiation (§9.3). Do not skip this.

Retcode **10030** (``TRADE_RETCODE_INVALID_FILL``, "Unsupported filling mode") is the
single most common first-day failure with MT5 Python, because the supported mode is
per-symbol and per-broker and the obvious default is often wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

from fxbot.core.errors import SymbolResolutionError
from fxbot.core.models import SymbolSpec

SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
SYMBOL_FILLING_BOC = 4
"""Book-or-cancel: passive/limit orders only, NEVER market orders."""

ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_BOC = 2
ORDER_FILLING_RETURN = 3

SYMBOL_TRADE_EXECUTION_MARKET = 2

_MASK_TO_ORDER = {SYMBOL_FILLING_IOC: ORDER_FILLING_IOC, SYMBOL_FILLING_FOK: ORDER_FILLING_FOK}
_PREFERENCE = (SYMBOL_FILLING_IOC, SYMBOL_FILLING_FOK)
"""Preference for market orders: IOC, then FOK."""


def filling_candidates(spec: SymbolSpec) -> list[int]:
    """Return the ordered list of ``ORDER_FILLING_*`` values to try for market orders.

    On retcode 10030 the caller retries with the next candidate and permanently caches the
    winner in ``state/filling_modes.json``.

    Args:
        spec: The symbol specification, whose ``filling_modes`` is the bitmask from
            ``symbol_info().filling_mode``.

    Returns:
        The candidates, best first. Never empty -- an empty list would be indistinguishable
        from "not checked yet" at the call site.

    Raises:
        SymbolResolutionError: If neither FOK nor IOC is set in the mask.

            ``ORDER_FILLING_RETURN`` is **not** a fallback. MQL5 disallows it whenever
            ``trade_exemode == SYMBOL_TRADE_EXECUTION_MARKET``, which is exactly what a
            raw-spread book like Pepperstone Razor uses -- so "fall back to RETURN" loops
            on 10030 forever. Failing closed here is the correct behaviour (§0.7).
    """
    candidates = [_MASK_TO_ORDER[bit] for bit in _PREFERENCE if spec.filling_modes & bit]
    if not candidates:
        raise SymbolResolutionError(
            f"{spec.name}: filling_mode bitmask {spec.filling_modes} offers neither IOC nor FOK "
            f"(BOC set: {bool(spec.filling_modes & SYMBOL_FILLING_BOC)}, "
            f"trade_exemode={spec.trade_exemode}). ORDER_FILLING_RETURN is not a fallback on a "
            "market-execution book; refusing to trade this symbol rather than looping on 10030."
        )
    return candidates


def negotiate_filling_mode(spec: SymbolSpec) -> int:
    """Return the preferred ``ORDER_FILLING_*`` value for market orders on ``spec``.

    Args:
        spec: The symbol specification.

    Returns:
        ``ORDER_FILLING_IOC`` when the symbol supports it, else ``ORDER_FILLING_FOK``.

    Raises:
        SymbolResolutionError: When the symbol supports neither.
    """
    return filling_candidates(spec)[0]


class FillingCache:
    """The confirmed working filling mode per ``(server, symbol)``.

    The *candidates* are derived from the symbol at startup and logged; the *working* mode
    is confirmed by the first real, risk-approved order and only then cached. Never probe
    by sending an unapproved order -- that would bypass ``RiskGovernor.approve()`` (§0.4).
    Never carry a demo cache into live: different server, possibly different symbol
    properties, which is why the server name is part of the key.
    """

    def __init__(self, path: Path, server: str) -> None:
        """Open (but do not yet read) the cache.

        Args:
            path: ``state/filling_modes.json``.
            server: The broker server name the cache is keyed by.
        """
        self._path = path
        self._server = server
        self._data: dict[str, int] = {}
        self._loaded = False

    def _key(self, symbol: str) -> str:
        return f"{self._server}|{symbol}"

    def load(self) -> None:
        """Read the cache from disk. A corrupt cache is discarded, not fatal.

        Discarding is safe here and only here: the worst case is one 10030 and a retry,
        because the negotiation always re-derives candidates from the live symbol spec.
        """
        self._loaded = True
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}
            return
        if isinstance(raw, dict):
            self._data = {str(k): int(v) for k, v in raw.items() if isinstance(v, int)}

    def get(self, symbol: str) -> int | None:
        """Return the cached working mode for ``symbol``, or None."""
        if not self._loaded:
            self.load()
        return self._data.get(self._key(symbol))

    def remember(self, symbol: str, mode: int) -> None:
        """Persist ``mode`` as the confirmed working mode for ``symbol``."""
        if not self._loaded:
            self.load()
        self._data[self._key(symbol)] = mode
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
