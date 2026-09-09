"""Shared fixtures (§12.4).

``MetaTrader5`` is mocked at the module boundary by :class:`FakeMT5`, which replays
recorded responses **including failure retcodes**. Nothing in this file mocks our own
code: a test that mocks ``position_size`` proves only that the mock works.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from fxbot.config.schema import AppConfig
from fxbot.core.clock import ServerClock
from fxbot.core.enums import RejectReason, Side
from fxbot.core.models import (
    AccountState,
    Bar,
    Position,
    QualityReport,
    StrategyContext,
    SymbolSpec,
)
from fxbot.ops.alerts import Alerter
from fxbot.runtime.journal import Journal

FIXTURES = Path(__file__).parent / "fixtures"
SERVER_TZ = timezone(timedelta(hours=3))
"""The fixtures were generated on a UTC+3 broker; every test clock uses that offset."""

GOLDEN_CASES = (
    "clean_long", "clean_short", "adx_blocked", "vol_ceiling", "bias_neutral",
    "donchian_lookahead",
)


def load_spec(symbol: str) -> SymbolSpec:
    """Load a captured symbol specification (§12.2)."""
    return SymbolSpec(**json.loads((FIXTURES / "specs" / f"{symbol}.json").read_text()))


def load_bars(name: str) -> pd.DataFrame:
    """Load a golden bar fixture as a server-time-indexed frame."""
    frame = pd.read_csv(FIXTURES / "bars" / f"{name}.csv", index_col="time",
                        parse_dates=["time"])
    frame.index = frame.index.tz_convert(SERVER_TZ)
    return frame


def frame_bars(frame: pd.DataFrame) -> list[Bar]:
    """Convert a frame to :class:`~fxbot.core.models.Bar` objects."""
    from fxbot.backtest.replay import frame_to_bars

    return frame_to_bars(frame)


@pytest.fixture
def clock() -> ServerClock:
    """A UTC+3 broker clock that has already observed a timestamp."""
    c = ServerClock(3)
    c.observe(datetime(2024, 5, 8, 12, 0, tzinfo=SERVER_TZ))
    return c


@pytest.fixture
def cfg() -> AppConfig:
    """A backtest configuration with the schema defaults."""
    return AppConfig(env="backtest")


@pytest.fixture
def eurusd() -> SymbolSpec:
    """The captured EURUSD Razor specification."""
    return load_spec("EURUSD")


@pytest.fixture
def usdjpy() -> SymbolSpec:
    """The captured USDJPY specification -- three digits, non-USD profit currency."""
    return load_spec("USDJPY")


@pytest.fixture
def specs() -> dict[str, SymbolSpec]:
    """Every captured specification, keyed by canonical symbol."""
    return {name: load_spec(name)
            for name in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD")}


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    """A throwaway SQLite journal."""
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


@pytest.fixture
def alerter() -> Alerter:
    """An alerter that records instead of sending."""
    return Alerter(enabled=False, min_severity="INFO", bot_token=None, chat_id=None,
                   env="test")


def make_context(
    frame: pd.DataFrame,
    spec: SymbolSpec,
    cfg: AppConfig,
    clock: ServerClock,
    position: Position | None = None,
    spread_points: int = 8,
    quality: QualityReport | None = None,
) -> StrategyContext:
    """Build a :class:`StrategyContext` the way the engine does, for a golden frame."""
    from fxbot.data.resample import resample_h1_to_d1

    now = frame.index[-1].to_pydatetime() + timedelta(minutes=cfg.timeframe_minutes)
    report = quality or QualityReport(True, RejectReason.NONE, "", len(frame), now, 0, False)
    return StrategyContext(
        symbol=spec.name, now=now, h1=frame, d1=resample_h1_to_d1(frame, clock, now),
        spec=spec, current_spread_points=spread_points, open_position=position,
        params=cfg.strategy, session=cfg.session,
        max_spread_points=cfg.execution.spread_cap(spec.name),
        commission_per_lot_round_turn=cfg.costs.commission_per_lot_round_turn,
        quality=report,
    )


def make_position(
    spec: SymbolSpec,
    side: Side,
    entry: float,
    stop: float,
    when: datetime,
    volume: float = 0.10,
    ticket: int = 1,
    stop_loss: float | None = None,
    partial_taken: bool = False,
) -> Position:
    """Build an open position for the management tests."""
    return Position(
        ticket=ticket, symbol=spec.name, side=side, volume=volume, entry_price=entry,
        stop_loss=stop if stop_loss is None else stop_loss, take_profit=0.0, open_time=when,
        profit=0.0, magic=990117, comment="test", initial_stop=stop,
        initial_volume=volume, partial_taken=partial_taken,
    )


def make_account(equity: float, when: datetime, balance: float | None = None) -> AccountState:
    """Build an account snapshot."""
    return AccountState(equity=equity, balance=balance if balance is not None else equity,
                        margin=0.0, margin_free=equity, currency="USD", leverage=400,
                        server_time=when)


class FakeMT5:
    """A stand-in for the ``MetaTrader5`` module that replays recorded responses.

    Every constant the adapters read is present, and every call can be scripted to return
    a failure retcode -- which is the point: the retry and filling paths are only worth
    testing against the failures a real terminal produces.
    """

    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    TIMEFRAME_H1 = 16385

    def __init__(self) -> None:
        """Start with an empty, connected terminal."""
        self.send_results: list[Any] = []
        self.check_result: Any = SimpleNamespace(retcode=0, comment="ok")
        self.requests: list[dict[str, Any]] = []
        self.positions: list[Any] = []
        self.deals: list[Any] = []
        self.orders: list[Any] = []
        self.symbols: list[Any] = []
        self.infos: dict[str, Any] = {}
        self.ticks: dict[str, Any] = {}
        self.account = SimpleNamespace(equity=10_000.0, balance=10_000.0, margin=0.0,
                                       margin_free=10_000.0, currency="USD", leverage=400)
        self.trade_allowed = True
        self.initialised = False
        self.margin_result: float | None = 50.0

    # -- lifecycle
    def initialize(self, **kwargs: Any) -> bool:
        """Pretend to start a terminal."""
        self.initialised = True
        return True

    def shutdown(self) -> None:
        """Pretend to stop."""
        self.initialised = False

    def terminal_info(self) -> Any:
        """Return the terminal state, including the Algo Trading switch."""
        return SimpleNamespace(trade_allowed=self.trade_allowed)

    def last_error(self) -> tuple[int, str]:
        """Return a plausible error tuple."""
        return (-1, "fake error")

    # -- symbols
    def symbols_get(self) -> list[Any]:
        """Return the scripted symbol universe."""
        return self.symbols

    def symbol_select(self, name: str, enable: bool) -> bool:
        """Select a symbol into Market Watch."""
        return name in self.infos

    def symbol_info(self, name: str) -> Any:
        """Return the scripted symbol info."""
        return self.infos.get(name)

    def symbol_info_tick(self, name: str) -> Any:
        """Return the scripted tick."""
        return self.ticks.get(name)

    def account_info(self) -> Any:
        """Return the scripted account."""
        return self.account

    # -- trading
    def order_check(self, request: Mapping[str, Any]) -> Any:
        """Return the scripted pre-trade check result."""
        return self.check_result

    def order_send(self, request: Mapping[str, Any]) -> Any:
        """Pop the next scripted send result and record the request."""
        self.requests.append(dict(request))
        if not self.send_results:
            return SimpleNamespace(retcode=10009, order=1, price=request.get("price", 0.0),
                                   volume=request.get("volume", 0.0), comment="done")
        return self.send_results.pop(0)

    def order_calc_margin(self, order_type: int, symbol: str, volume: float,
                          price: float) -> float | None:
        """Return the scripted margin requirement."""
        return self.margin_result

    def positions_get(self, **kwargs: Any) -> list[Any]:
        """Return the scripted positions, filtered by ticket or symbol if asked."""
        if "ticket" in kwargs:
            return [p for p in self.positions if p.ticket == kwargs["ticket"]]
        if "symbol" in kwargs:
            return [p for p in self.positions if p.symbol == kwargs["symbol"]]
        return list(self.positions)

    def history_deals_get(self, *args: Any, **kwargs: Any) -> list[Any]:
        """Return the scripted deal history."""
        return list(self.deals)

    def history_orders_get(self, *args: Any, **kwargs: Any) -> list[Any]:
        """Return the scripted order history."""
        return list(self.orders)


def symbol_info_tuple(spec: SymbolSpec, trade_mode: int = 4) -> SimpleNamespace:
    """Render a :class:`SymbolSpec` back into the shape ``mt5.symbol_info()`` returns."""
    return SimpleNamespace(
        name=spec.name, digits=spec.digits, point=spec.point, trade_tick_size=spec.tick_size,
        trade_tick_value_loss=spec.tick_value, trade_tick_value_profit=spec.tick_value_profit,
        trade_contract_size=spec.contract_size, swap_long=spec.swap_long,
        swap_short=spec.swap_short, swap_mode=spec.swap_mode, trade_exemode=spec.trade_exemode,
        currency_base=spec.currency_base, volume_min=spec.volume_min,
        volume_max=spec.volume_max, volume_step=spec.volume_step,
        trade_stops_level=spec.stops_level, trade_freeze_level=spec.freeze_level,
        filling_mode=spec.filling_modes, currency_profit=spec.currency_profit,
        currency_margin=spec.currency_margin, trade_mode=trade_mode,
    )


@pytest.fixture
def fake_mt5() -> FakeMT5:
    """A fresh :class:`FakeMT5`."""
    return FakeMT5()
