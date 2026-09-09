"""THE shared fill model (§11.2, §12.5).

Implemented once here and imported by **both** the Backtrader adapter and
:class:`~fxbot.execution.paper_broker.PaperBroker`. Without a single shared model every
long entry would differ by the full spread and the keystone parity test could never pass.

The contract, stated once so both engines can be checked against it:

* **Bars are bid.** MT5 FX bars are bid quotes, so every buy-side execution -- a long
  entry and a short exit alike -- pays the spread, and every sell-side execution does not.
* **Market orders fill at the next bar's open**, never the signal bar's close. The live
  engine decides after the close and sends immediately; the backtest must not do better.
* **Slippage is adverse**: added on a buy, subtracted on a sell.
* **Commission is charged per side**, ``commission_per_lot_per_side * volume`` each way.
* **A gap through the stop fills at the bar's open**, not at the stop price. This is where
  weekend gaps actually cost money and pretending otherwise flatters every result.
* **A stop submitted on bar ``i`` is first checked on bar ``i + 1``.** Backtrader cannot
  check an order on the bar it was submitted on, so neither does the paper broker.

**Known optimism, stated plainly (§18).** A short position's stop is a buy-stop and in
reality triggers on the *ask*, but historical bars carry only bid extremes, so the trigger
is tested against ``bar.high``. The *fill* still pays the full spread. The trigger is
therefore one spread late for shorts -- about 0.1 pip on Razor EURUSD. It is not silently
absorbed: the 1.5x-spread stress run §11.2 requires is exactly where it shows up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fxbot.core.enums import Side
from fxbot.core.models import Bar, SymbolSpec

_WEDNESDAY = 2
_SATURDAY = 5
_SUNDAY = 6
_TRIPLE_SWAP_WEEKDAY = 3
"""Rolling into Thursday carries Wednesday's triple swap."""

SWAP_MODE_POINTS = 0
"""``SYMBOL_SWAP_MODE_POINTS`` -- swap quoted in points. The only mode modelled."""


@dataclass(frozen=True, slots=True)
class FillModel:
    """Turns a bar plus an order into an execution price and a cost."""

    spec: SymbolSpec
    commission_per_lot_per_side: float
    slippage_points: int
    spread_source: str = "historical"
    fixed_spread_points: int = 8
    spread_multiplier: float = 1.0
    """Stress knob: §11.2 requires a run at 1.5x spread."""
    slippage_multiplier: float = 1.0
    """Stress knob: §11.2 requires a run at 2x slippage."""

    # ------------------------------------------------------------------ prices

    def spread_price(self, bar_spread_points: int) -> float:
        """Return the spread for a bar in price units.

        Args:
            bar_spread_points: The ``spread`` column MT5 provides per bar.

        Returns:
            The modelled spread in price units, after the stress multiplier.
        """
        points = (bar_spread_points if self.spread_source == "historical"
                  else self.fixed_spread_points)
        return max(points, 0) * self.spec.point * self.spread_multiplier

    def slippage_price(self) -> float:
        """Return the modelled slippage in price units.

        Backtrader's ``set_slippage_fixed(fixed=...)`` takes **absolute price units, not
        points**. Passing a raw ``3`` slips every EURUSD fill by 3.00 -- 100,000x too much
        -- and silently invalidates every result and therefore the §11.4 gate. The
        multiplication by ``spec.point`` happens here, once (§11.2).
        """
        return self.slippage_points * self.spec.point * self.slippage_multiplier

    def buy_adjustment(self, bar_spread_points: int) -> float:
        """Price added to any buy-side execution: the spread plus adverse slippage."""
        return self.spread_price(bar_spread_points) + self.slippage_price()

    def sell_adjustment(self) -> float:
        """Price subtracted from any sell-side execution: adverse slippage only."""
        return self.slippage_price()

    def market_fill(self, side: Side, bar: Bar) -> float:
        """Return the fill price of a market order executing on ``bar``.

        Args:
            side: The direction of the *execution*, not of the position. Closing a long is
                a SELL.
            bar: The bar the order executes on -- the one after the decision bar.

        Returns:
            The fill price.
        """
        if side is Side.BUY:
            return bar.open + self.buy_adjustment(bar.spread)
        return bar.open - self.sell_adjustment()

    def stop_fill(self, position_side: Side, bar: Bar, stop: float) -> float | None:
        """Return the fill price if ``bar`` triggers the stop, else None.

        Args:
            position_side: The side of the position the stop protects.
            bar: The bar to test.
            stop: The stop price currently on the position.

        Returns:
            The fill price, or None when the bar never reached the stop.
        """
        if stop <= 0.0:
            return None
        if position_side is Side.BUY:
            if bar.low > stop:
                return None
            base = bar.open if bar.open <= stop else stop
            return base - self.sell_adjustment()
        if bar.high < stop:
            return None
        base = bar.open if bar.open >= stop else stop
        return base + self.buy_adjustment(bar.spread)

    # ------------------------------------------------------------------ costs

    def commission(self, volume: float) -> float:
        """Return the commission for one side of a trade of ``volume`` lots."""
        return self.commission_per_lot_per_side * volume

    def swap(self, side: Side, volume: float, entry_time: datetime, exit_time: datetime) -> float:
        """Return the financing cost of holding ``volume`` lots between the two times.

        H1 holds routinely last days, so swap is not optional (§11.5). Charged once per
        rollover into a weekday, **tripled rolling into Thursday** because that is when
        the broker books the weekend. Rollovers into Saturday and Sunday are free -- the
        triple Wednesday is what pays for them.

        Args:
            side: The position side.
            volume: Position volume in lots.
            entry_time: Fill time, broker server time.
            exit_time: Exit time, broker server time.

        Returns:
            The swap in account currency: negative is a cost. Zero when the symbol quotes
            swaps in a mode other than points -- see the note below.
        """
        if self.spec.swap_mode != SWAP_MODE_POINTS:
            # Modes other than "points" (base currency, interest, margin currency) need a
            # conversion the historical file does not carry. Returning 0.0 here would
            # understate costs, which §17.17 forbids -- so `runner.py` refuses to report a
            # backtest for a symbol whose swap_mode is unmodelled rather than quietly
            # dropping the term. This branch exists so that check has something to test.
            return 0.0

        points = self.spec.swap_long if side is Side.BUY else self.spec.swap_short
        if points == 0.0:
            return 0.0
        nights = self.swap_nights(entry_time, exit_time)
        per_night = points * self.spec.point * self.spec.value_per_price_unit_per_lot * volume
        return per_night * nights

    @staticmethod
    def swap_nights(entry_time: datetime, exit_time: datetime) -> int:
        """Return the weighted number of rollovers between two server times.

        Args:
            entry_time: Fill time, broker server time.
            exit_time: Exit time, broker server time.

        Returns:
            The count of rollovers, with the roll into Thursday counted three times and
            rolls into Saturday and Sunday not counted at all.
        """
        if exit_time <= entry_time:
            return 0
        weighted = 0
        day: date = entry_time.date() + timedelta(days=1)
        last: date = exit_time.date()
        while day <= last:
            weekday = day.weekday()
            if weekday == _TRIPLE_SWAP_WEEKDAY:
                weighted += 3
            elif weekday not in (_SATURDAY, _SUNDAY):
                weighted += 1
            day += timedelta(days=1)
        return weighted


def floating_pnl(side: Side, volume: float, entry_price: float, mark: float,
                 spec: SymbolSpec) -> float:
    """Return the unrealised P/L of a position in account currency.

    Marked at the bar close on both sides, which is what Backtrader's own valuation does;
    marking longs to bid and shorts to ask would make the two engines' equity -- and
    therefore their position sizes -- diverge for no analytical gain.

    Args:
        side: Position side.
        volume: Volume in lots.
        entry_price: Entry price.
        mark: The price to mark at, normally the last close.
        spec: The symbol specification.

    Returns:
        Unrealised P/L in account currency.
    """
    return (mark - entry_price) * side.sign * volume * spec.value_per_price_unit_per_lot


def realised_pnl(side: Side, volume: float, entry_price: float, exit_price: float,
                 spec: SymbolSpec) -> float:
    """Return the gross P/L of a closed leg, before commission and swap."""
    return (exit_price - entry_price) * side.sign * volume * spec.value_per_price_unit_per_lot


class AccountBook:
    """Cash and equity accounting shared by both engines.

    **Why this is shared (§12.5).** The governor sizes from equity, so if the two engines
    valued the account differently they could round to different lot sizes and diverge on
    a trade neither disagreed about. Backtrader still *executes* the orders -- §11.1's
    adapter rule is intact -- but both engines are handed their equity by this one class,
    so the number cannot drift.
    """

    def __init__(self, starting_cash: float, currency: str = "USD") -> None:
        """Open a book.

        Args:
            starting_cash: Opening balance in account currency.
            currency: Account currency, carried through to ``AccountState``.
        """
        self.starting_cash = starting_cash
        self.balance = starting_cash
        self.currency = currency

    def charge(self, amount: float) -> None:
        """Deduct a cost (commission) from the balance."""
        self.balance -= amount

    def credit(self, amount: float) -> None:
        """Add a realised amount (P/L or swap; negative is a cost) to the balance."""
        self.balance += amount

    def equity(self, floating: float) -> float:
        """Return balance plus open floating P/L."""
        return self.balance + floating
