"""Broker-server time (§6.3).

The VPS runs UTC, the operator is in Nairobi (UTC+3) and the broker server keeps its own
offset (typically UTC+2/UTC+3 with DST). Every day boundary and session window in this
system is expressed in **broker server time**, derived from tick timestamps -- never from
the OS clock, and never from Nairobi or UTC midnight.

**Ambiguity resolved (§18).** §6.3 fixes the constructor signature as
``__init__(self, offset_hours: int)`` and §0.2 bans wall-clock reads from ``core/``, yet
:meth:`ServerClock.now` must return "now". Three readings are possible: read the OS clock
here (violates §0.2), inject a callable into the constructor (violates the verbatim
signature), or make the clock a *record* of the most recent broker timestamp it has been
shown. The third is chosen: :meth:`observe` is fed broker timestamps by the adapter layer
(``AccountState.server_time``, tick times), and :meth:`now` replays the latest. That is
literally what §6.3 asks for -- "built from broker tick timestamps, never from the OS
clock" -- and it keeps ``core/`` pure. :meth:`from_utc` converts a true-UTC instant
supplied by the injected ``utc_now`` callable (§0.6) into server time for the scheduler.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fxbot.core.errors import ClockError

if TYPE_CHECKING:  # pragma: no cover - typing only, see the module note in core/models.py
    from fxbot.config.schema import SessionParams

_SUNDAY = 6
_MONDAY = 0
_FRIDAY = 4
_SATURDAY = 5


class ServerClock:
    """The ONLY source of "now" for trading logic."""

    __slots__ = ("_last_observed", "_offset_hours", "_tz")

    def __init__(self, offset_hours: int) -> None:
        """Build a clock for a broker whose server runs at ``UTC+offset_hours``.

        Args:
            offset_hours: Whole-hour offset of the broker server from UTC, as returned by
                :func:`fxbot.data.clock_probe.probe_server_offset`.
        """
        self._offset_hours = int(offset_hours)
        self._tz = timezone(timedelta(hours=self._offset_hours))
        self._last_observed: datetime | None = None

    @property
    def offset_hours(self) -> int:
        """Whole-hour offset of the broker server from UTC."""
        return self._offset_hours

    @property
    def tz(self) -> timezone:
        """The fixed ``tzinfo`` every server-time datetime in the system carries."""
        return self._tz

    def localize(self, naive_server_time: datetime) -> datetime:
        """Attach the server timezone to a naive timestamp that is already server time.

        MT5 hands out naive timestamps that are server-local; this is the one place that
        knowledge is applied.

        Args:
            naive_server_time: A naive datetime already expressed in server time.

        Returns:
            The same wall-clock instant, tz-aware in server time.

        Raises:
            ClockError: If the argument is already timezone-aware.
        """
        if naive_server_time.tzinfo is not None:
            raise ClockError("localize() expects a naive server timestamp")
        return naive_server_time.replace(tzinfo=self._tz)

    def from_utc(self, utc_time: datetime) -> datetime:
        """Convert a true-UTC instant into broker server time.

        Args:
            utc_time: A tz-aware UTC instant, supplied by the injected ``utc_now``
                callable (§0.6). Naive input is rejected rather than guessed at.

        Returns:
            The same instant, tz-aware in server time.

        Raises:
            ClockError: If ``utc_time`` is naive.
        """
        if utc_time.tzinfo is None:
            raise ClockError("from_utc() requires a timezone-aware UTC datetime")
        return utc_time.astimezone(self._tz)

    def observe(self, server_time: datetime) -> None:
        """Record the latest timestamp seen from the broker.

        Args:
            server_time: A tz-aware broker-server timestamp (a tick time, a bar time or
                ``AccountState.server_time``). Monotonicity is not enforced: brokers do
                re-send timestamps, and rejecting them would be a fail-open behaviour.

        Raises:
            ClockError: If ``server_time`` is naive.
        """
        if server_time.tzinfo is None:
            raise ClockError("observe() requires a timezone-aware server datetime")
        self._last_observed = server_time.astimezone(self._tz)

    def now(self) -> datetime:
        """Return the most recent broker-server time this clock has been shown.

        Returns:
            Tz-aware server time.

        Raises:
            ClockError: If no broker timestamp has been observed yet. Failing here is
                deliberate: a clock that has never seen the broker must not be allowed to
                invent a time and drive a day boundary off it (§0.7).
        """
        if self._last_observed is None:
            raise ClockError("ServerClock.now() called before any broker timestamp was observed")
        return self._last_observed

    def trading_day(self, t: datetime) -> date:
        """Return the broker day ``t`` belongs to.

        The broker day is ``[00:00 server, 24:00 server)``. This is the boundary the daily
        loss limit resets on -- NOT Nairobi midnight, NOT UTC midnight.

        Args:
            t: A tz-aware datetime in any timezone.

        Returns:
            The broker-server calendar date.
        """
        return t.astimezone(self._tz).date()

    def is_new_trading_day(self, prev: datetime, now: datetime) -> bool:
        """Return whether ``now`` falls in a strictly later broker day than ``prev``.

        Strictly later, not merely different: a DST transition that moves the server clock
        backwards must not be read as a new day and trigger a second daily reset.

        Args:
            prev: Server time of the previous cycle.
            now: Server time of the current cycle.

        Returns:
            True when a daily reset is due.
        """
        return self.trading_day(now) > self.trading_day(prev)

    def next_bar_close(self, t: datetime, tf_minutes: int) -> datetime:
        """Return the first bar close strictly after ``t``.

        Args:
            t: A tz-aware server time.
            tf_minutes: Timeframe length in minutes (60 for H1).

        Returns:
            The next instant that is an exact multiple of ``tf_minutes`` past server
            midnight and strictly greater than ``t``.

        Raises:
            ClockError: If ``tf_minutes`` is not positive.
        """
        if tf_minutes <= 0:
            raise ClockError(f"tf_minutes must be positive, got {tf_minutes}")
        local = t.astimezone(self._tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = (local - midnight).total_seconds()
        step = tf_minutes * 60
        n = int(elapsed // step) + 1
        return midnight + timedelta(seconds=n * step)

    def in_session(self, t: datetime, s: SessionParams) -> bool:
        """Return whether new entries are permitted at server time ``t`` (§7.3 step 3).

        **Ambiguity resolved (§18).** "the first N hours of the week" is not defined by a
        broker-independent rule, because servers open somewhere between Sunday 22:00 and
        Monday 00:00 server time. The trading week is therefore taken to open at Monday
        00:00 server; Sunday bars are treated as inside the skip window whenever
        ``skip_hours_after_weekend_open > 0``. "Friday after hour H" is read strictly:
        hour ``H`` itself still trades, ``H + 1`` does not.

        Args:
            t: Server time of the just-closed bar's close.
            s: Session parameters from config.

        Returns:
            True when the session gate passes.
        """
        local = t.astimezone(self._tz)
        weekday = local.weekday()

        if weekday == _SATURDAY:
            return False
        if local.hour not in s.trade_hours_server:
            return False
        if weekday == _FRIDAY and local.hour > s.skip_friday_after_hour:
            return False
        if s.skip_hours_after_weekend_open > 0:
            if weekday == _SUNDAY:
                return False
            if weekday == _MONDAY and local.hour < s.skip_hours_after_weekend_open:
                return False
        return True

    def __repr__(self) -> str:
        """Return a debug representation naming the offset."""
        return f"ServerClock(offset_hours={self._offset_hours})"

