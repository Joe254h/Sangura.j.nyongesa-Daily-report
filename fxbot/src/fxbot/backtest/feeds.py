"""Backtrader data feeds and the cost-aware broker (§11.2).

The broker subclass exists so that Backtrader's fills come out of exactly the same
:class:`~fxbot.backtest.costs.FillModel` the paper broker uses. ``_slip_up`` and
``_slip_down`` are Backtrader's own extension points for custom execution pricing; every
market and stop execution funnels through one of them, so overriding the pair is enough to
own the whole price surface without touching ``_execute``.
"""

from __future__ import annotations

from typing import Any, cast

import backtrader as bt
import pandas as pd

from fxbot.backtest.costs import FillModel


class FxData(bt.feeds.PandasData):  # type: ignore[misc]
    """A Pandas feed carrying MT5's per-bar ``spread`` column as an extra line.

    The spread has to reach the broker: it is what a buy-side execution pays, and using a
    fixed average instead would flatter every entry taken during a news bar.
    """

    lines = ("spread",)
    params = (
        ("datetime", None),
        ("open", "open"),
        ("high", "high"),
        ("low", "low"),
        ("close", "close"),
        ("volume", "volume"),
        ("openinterest", None),
        ("spread", "spread"),
    )


def make_feed(frame: pd.DataFrame, name: str) -> FxData:
    """Build a Backtrader feed from a bar frame.

    Args:
        frame: Ascending bars indexed by open time in server time.
        name: The symbol name Backtrader will report.

    Returns:
        The feed.
    """
    prepared = frame.copy()
    if "spread" not in prepared.columns:
        prepared["spread"] = 0
    if "volume" not in prepared.columns:
        prepared["volume"] = 0
    # Backtrader's datetime handling is naive; the tz is carried by the ServerClock, and
    # every timestamp that leaves the adapter is re-localised from it.
    prepared.index = cast(pd.DatetimeIndex, prepared.index).tz_localize(None)
    return FxData(dataname=prepared, name=name)


class CostBroker(bt.brokers.BackBroker):  # type: ignore[misc]
    """A Backtrader broker whose execution prices come from the shared fill model.

    ``slip_out`` semantics are taken as given: the adjusted price is returned even when it
    falls outside the bar's range. That is deliberate and it is what really happens -- a
    stop that gaps through fills where the market is, not where the bar's low was.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the broker with no fill model yet; call :meth:`set_fill_models`."""
        super().__init__(*args, **kwargs)
        self._models: dict[str, FillModel] = {}
        self._current: Any = None

    def set_fill_models(self, models: dict[str, FillModel]) -> None:
        """Register ``symbol -> FillModel``."""
        self._models = dict(models)

    def _try_exec(self, order: Any) -> Any:
        """Remember which data the order belongs to, then defer to Backtrader."""
        self._current = order.data
        return super()._try_exec(order)

    def _adjustment(self, buying: bool) -> float:
        """Return the price adjustment for the data currently being executed."""
        data = self._current
        if data is None:
            return 0.0
        model = self._models.get(data._name)
        if model is None:
            return 0.0
        spread_points = int(data.spread[0]) if len(data.spread) else 0
        return model.buy_adjustment(spread_points) if buying else model.sell_adjustment()

    def _slip_up(self, pmax: float, price: float,  # noqa: ARG002
                 doslip: bool = True, lim: bool = False) -> float:  # noqa: ARG002
        """Return the buy-side execution price: bar price + spread + adverse slippage.

        ``pmax``, ``doslip`` and ``lim`` are Backtrader's clamping arguments and are
        deliberately ignored: the shared fill model already decides the price, and clamping
        it back inside the bar would stop a gap through a stop from filling where the
        market actually is.
        """
        return price + self._adjustment(buying=True)

    def _slip_down(self, pmin: float, price: float,  # noqa: ARG002
                   doslip: bool = True, lim: bool = False) -> float:  # noqa: ARG002
        """Return the sell-side execution price: bar price - adverse slippage.

        See :meth:`_slip_up` for why the clamping arguments are ignored.
        """
        return price - self._adjustment(buying=False)


class FxCommission(bt.CommInfoBase):  # type: ignore[misc]
    """Fixed commission per lot per side, with FX-correct P/L scaling.

    ``mult`` is the symbol's account-currency value per full price unit per lot
    (``tick_value / tick_size``), which generalises past the "$10 a pip" assumption §6.2
    warns about and is right for JPY crosses and non-USD-quoted pairs alike.
    """

    params = (
        ("commtype", bt.CommInfoBase.COMM_FIXED),
        ("stocklike", False),
        ("percabs", True),
    )
