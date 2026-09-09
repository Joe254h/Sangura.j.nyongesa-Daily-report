"""``ReplayDataSource`` -- the live engine's data adapter, backed by fixture bars (§12.5).

This is what lets ``test_parity.py`` drive the **real** :class:`TradingEngine` over historic
data: the engine cannot tell this apart from
:class:`~fxbot.data.mt5_source.MT5DataSource`, which is the point. It also backs
``fxbot backtest --engine replay`` and the dry-run soak in §13.5 step 2.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import cast

import pandas as pd

from fxbot.core.clock import ServerClock
from fxbot.core.errors import DataUnavailableError
from fxbot.core.models import AccountState, Bar, SymbolSpec


def frame_to_bars(frame: pd.DataFrame) -> list[Bar]:
    """Convert a bar frame into the frozen :class:`~fxbot.core.models.Bar` list.

    Args:
        frame: Ascending bars indexed by open time in server time.

    Returns:
        One :class:`Bar` per row, in order.
    """
    return [
        Bar(time=cast(pd.Timestamp, index).to_pydatetime(),
            open=float(cast(float, row.open)), high=float(cast(float, row.high)),
            low=float(cast(float, row.low)), close=float(cast(float, row.close)),
            volume=int(getattr(row, "volume", 0) or 0),
            spread=int(getattr(row, "spread", 0) or 0))
        for index, row in zip(frame.index, frame.itertuples(), strict=True)
    ]


class ReplayDataSource:
    """Serves closed bars, ticks and the account from an in-memory history."""

    def __init__(
        self,
        frames: Mapping[str, pd.DataFrame],
        specs: Mapping[str, SymbolSpec],
        clock: ServerClock,
        account_provider: object,
        timeframe_minutes: int = 60,
    ) -> None:
        """Build the replay source.

        Args:
            frames: ``symbol -> full ascending bar frame``.
            specs: ``symbol -> SymbolSpec``.
            clock: The broker clock.
            account_provider: Anything with an ``account() -> AccountState`` method,
                normally the :class:`~fxbot.execution.paper_broker.PaperBroker` -- the
                simulated account is the one the governor must size from.
            timeframe_minutes: Bar length.
        """
        self._frames = dict(frames)
        self._specs = dict(specs)
        self._clock = clock
        self._account_provider = account_provider
        self._tf = timeframe_minutes
        self._cursor: dict[str, int] = dict.fromkeys(self._frames, -1)

    # ------------------------------------------------------------------ harness

    def seek(self, symbol: str, index: int) -> None:
        """Expose bars ``0..index`` inclusive for ``symbol``.

        Everything after ``index`` is invisible to the engine, which is what enforces
        "closed bars only" (§0.3) structurally rather than by convention.
        """
        self._cursor[symbol] = index

    def seek_all(self, index: int) -> None:
        """Seek every symbol to the same index."""
        for symbol in self._frames:
            self._cursor[symbol] = index

    def length(self, symbol: str) -> int:
        """Return the number of bars available for ``symbol``."""
        return len(self._frames[symbol])

    def bar_at(self, symbol: str, index: int) -> Bar:
        """Return one bar by index, for the harness that drives the paper broker."""
        return frame_to_bars(self._frames[symbol].iloc[index:index + 1])[0]

    # ------------------------------------------------------------------ DataSource port

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        """Return the specification for ``symbol``."""
        return self._specs[symbol]

    def bars(self, symbol: str, timeframe: int, count: int) -> pd.DataFrame:  # noqa: ARG002
        """Return the last ``count`` closed bars up to the current cursor.

        ``timeframe`` is part of the DataSource port and is ignored here: a replay holds
        one timeframe per frame by construction.

        Raises:
            DataUnavailableError: Before the first :meth:`seek`, or when the symbol is
                unknown -- the same failure the live source raises, so the engine's
                handling is exercised identically.
        """
        frame = self._frames.get(symbol)
        if frame is None:
            raise DataUnavailableError(f"replay has no history for {symbol}")
        cursor = self._cursor.get(symbol, -1)
        if cursor < 0:
            raise DataUnavailableError(f"replay cursor for {symbol} is before the first bar")
        start = max(0, cursor + 1 - count)
        return frame.iloc[start:cursor + 1]

    def tick(self, symbol: str) -> tuple[float, float, datetime]:
        """Return ``(bid, ask, server_time)`` synthesised from the current bar.

        Bars are bid, so the bid is the bar close and the ask is the close plus the bar's
        own spread -- the same relationship :mod:`fxbot.backtest.costs` prices fills with.
        """
        frame = self._frames.get(symbol)
        cursor = self._cursor.get(symbol, -1)
        if frame is None or cursor < 0:
            raise DataUnavailableError(f"no tick available for {symbol} at cursor {cursor}")
        row = frame.iloc[cursor]
        spec = self._specs[symbol]
        bid = float(row["close"])
        ask = bid + int(row.get("spread", 0)) * spec.point
        when = frame.index[cursor].to_pydatetime() + timedelta(minutes=self._tf)
        return bid, ask, when

    def account(self) -> AccountState:
        """Return the simulated account, stamped with the current bar close."""
        state: AccountState = self._account_provider.account()  # type: ignore[attr-defined]
        symbol = next(iter(self._frames))
        cursor = self._cursor.get(symbol, -1)
        if cursor < 0:
            return state
        when = self._frames[symbol].index[cursor].to_pydatetime() + timedelta(minutes=self._tf)
        return AccountState(
            equity=state.equity, balance=state.balance, margin=state.margin,
            margin_free=state.margin_free, currency=state.currency, leverage=state.leverage,
            server_time=when,
        )

    def connect(self, secrets: Mapping[str, str] | None = None) -> None:
        """No-op: replay has nothing to connect to."""

    def shutdown(self) -> None:
        """No-op."""

    def resolve_symbols(self, wanted: Sequence[str]) -> dict[str, str]:
        """Identity mapping: fixture symbols are already broker symbols."""
        return {name: name for name in wanted}
