"""Broker-clock tests (§12.3).

The broker day boundary is 00:00 **server** time -- not 00:00 EAT, not 00:00 UTC. Getting
this wrong silently moves every session window and daily reset by hours.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from tests.conftest import SERVER_TZ, make_context

from fxbot.core.clock import ServerClock
from fxbot.core.errors import ClockError
from fxbot.data.clock_probe import probe_server_offset
from fxbot.strategy.trend_donchian import in_session

NAIROBI = timezone(timedelta(hours=3))
UTC = UTC


def test_now_refuses_to_invent_a_time() -> None:
    """A clock that has never seen the broker must not drive a day boundary (§0.7)."""
    with pytest.raises(ClockError):
        ServerClock(3).now()


def test_observe_and_now_round_trip() -> None:
    """``now()`` replays the most recent broker timestamp, in server time."""
    clock = ServerClock(2)
    stamp = datetime(2024, 6, 3, 15, 30, tzinfo=timezone(timedelta(hours=2)))
    clock.observe(stamp)
    assert clock.now() == stamp
    with pytest.raises(ClockError):
        clock.observe(datetime(2024, 6, 3, 15, 30))


def test_trading_day_is_the_server_day_not_utc_or_nairobi() -> None:
    """23:30 UTC on a UTC+3 server is already the next broker day."""
    clock = ServerClock(3)
    instant = datetime(2024, 6, 3, 23, 30, tzinfo=UTC)
    assert instant.astimezone(UTC).date() == date(2024, 6, 3)
    assert clock.trading_day(instant) == date(2024, 6, 4)


def test_broker_day_boundary_is_midnight_server() -> None:
    """23:59:59 server belongs to today; 00:00:00 server to tomorrow."""
    clock = ServerClock(3)
    before = datetime(2024, 6, 3, 23, 59, 59, tzinfo=SERVER_TZ)
    after = datetime(2024, 6, 4, 0, 0, 0, tzinfo=SERVER_TZ)
    assert clock.trading_day(before) == date(2024, 6, 3)
    assert clock.trading_day(after) == date(2024, 6, 4)
    assert clock.is_new_trading_day(before, after)


def test_a_clock_moving_backwards_does_not_double_reset_the_day() -> None:
    """A DST step back must not read as a new day and fire a second daily reset (§12.3)."""
    clock = ServerClock(3)
    # The server steps back an hour inside one day: not a new day.
    assert clock.is_new_trading_day(datetime(2024, 10, 27, 1, 30, tzinfo=SERVER_TZ),
                                    datetime(2024, 10, 27, 0, 30, tzinfo=SERVER_TZ)) is False
    # The server steps back across midnight after the day already rolled: still not a new
    # day, because `is_new_trading_day` compares strictly later, not merely different.
    # Comparing with != here would fire a second daily reset and wipe day_start_equity.
    assert clock.is_new_trading_day(datetime(2024, 10, 27, 0, 30, tzinfo=SERVER_TZ),
                                    datetime(2024, 10, 26, 23, 30, tzinfo=SERVER_TZ)) is False
    # A genuine forward rollover still registers exactly once.
    assert clock.is_new_trading_day(datetime(2024, 10, 26, 22, 30, tzinfo=SERVER_TZ),
                                    datetime(2024, 10, 27, 0, 30, tzinfo=SERVER_TZ)) is True


def test_dst_offset_change_keeps_one_reset_per_day() -> None:
    """Rebuilding the clock at a new offset does not manufacture an extra rollover."""
    winter = ServerClock(2)
    summer = ServerClock(3)
    last_cycle = datetime(2024, 3, 31, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    now = datetime(2024, 3, 31, 2, 30, tzinfo=timezone(timedelta(hours=3)))
    assert winter.trading_day(last_cycle) == date(2024, 3, 31)
    assert summer.trading_day(now) == date(2024, 3, 31)
    assert summer.is_new_trading_day(last_cycle, now) is False


def test_next_bar_close_is_strictly_after_now() -> None:
    """Exactly on the hour, the next close is the following hour, never this one."""
    clock = ServerClock(3)
    on_the_hour = datetime(2024, 6, 3, 14, 0, tzinfo=SERVER_TZ)
    assert clock.next_bar_close(on_the_hour, 60) == on_the_hour + timedelta(hours=1)
    mid = datetime(2024, 6, 3, 14, 17, 30, tzinfo=SERVER_TZ)
    assert clock.next_bar_close(mid, 60) == datetime(2024, 6, 3, 15, 0, tzinfo=SERVER_TZ)
    with pytest.raises(ClockError):
        clock.next_bar_close(mid, 0)


def test_session_gate_matches_between_clock_and_strategy(cfg, clock, eurusd) -> None:
    """The pure strategy's session gate must agree with the clock's, hour for hour.

    Two implementations exist because no clock object may cross the purity boundary
    (§0.2). That is only safe if they never disagree, so the equivalence is asserted here
    across a full week rather than assumed.
    """
    from tests.conftest import load_bars

    frame = load_bars("clean_long")
    session = cfg.session
    start = datetime(2024, 6, 2, 0, 0, tzinfo=SERVER_TZ)   # a Sunday
    for hours_ahead in range(24 * 7):
        moment = start + timedelta(hours=hours_ahead)
        ctx = make_context(frame, eurusd, cfg, clock)
        ctx = type(ctx)(**{**{f.name: getattr(ctx, f.name)
                              for f in ctx.__dataclass_fields__.values()}, "now": moment})
        assert in_session(ctx) == clock.in_session(moment, session), moment


def test_session_rejects_saturday_and_the_weekend_open(cfg, clock) -> None:
    """Saturday never trades; Monday's first hours and Sunday are inside the skip window."""
    session = cfg.session
    saturday = datetime(2024, 6, 8, 10, tzinfo=SERVER_TZ)
    sunday = datetime(2024, 6, 9, 10, tzinfo=SERVER_TZ)
    monday_early = datetime(2024, 6, 10, 1, tzinfo=SERVER_TZ)
    monday_open = datetime(2024, 6, 10, 9, tzinfo=SERVER_TZ)
    assert not clock.in_session(saturday, session)
    assert not clock.in_session(sunday, session)
    assert not clock.in_session(monday_early, session)
    assert clock.in_session(monday_open, session)


def test_session_rejects_friday_after_the_configured_hour(cfg, clock) -> None:
    """"After hour 19" is strict: 19:00 still trades, 20:00 does not."""
    session = cfg.session
    friday = datetime(2024, 6, 7, 19, tzinfo=SERVER_TZ)
    assert clock.in_session(friday, session)
    assert not clock.in_session(friday.replace(hour=20), session)


def test_probe_server_offset_agrees_across_samples() -> None:
    """Three consistent ticks give the offset."""
    class Source:
        def tick(self, symbol):  # noqa: ANN001, ANN202
            return 1.1, 1.1, datetime(2024, 6, 3, 15, 0, tzinfo=SERVER_TZ)

    def utc_now() -> datetime:
        return datetime(2024, 6, 3, 12, 0, tzinfo=UTC)

    assert probe_server_offset(Source(), "EURUSD", utc_now, samples=3, sleep_s=0.0) == 3


def test_probe_server_offset_refuses_to_average_disagreement() -> None:
    """Samples that disagree are a fault, not something to average (§6.3)."""
    times = [datetime(2024, 6, 3, 15, 0, tzinfo=SERVER_TZ),
             datetime(2024, 6, 3, 17, 0, tzinfo=SERVER_TZ),
             datetime(2024, 6, 3, 15, 0, tzinfo=SERVER_TZ)]

    class Source:
        def __init__(self) -> None:
            self.i = 0

        def tick(self, symbol):  # noqa: ANN001, ANN202
            when = times[self.i]
            self.i += 1
            return 1.1, 1.1, when

    with pytest.raises(ClockError, match="disagree"):
        probe_server_offset(Source(), "EURUSD", lambda: datetime(2024, 6, 3, 12, tzinfo=UTC),
                            samples=3, sleep_s=0.0)


def test_probe_server_offset_requires_aware_utc() -> None:
    """A naive ``utc_now`` is rejected rather than assumed to be UTC."""
    class Source:
        def tick(self, symbol):  # noqa: ANN001, ANN202
            return 1.1, 1.1, datetime(2024, 6, 3, 15, 0, tzinfo=SERVER_TZ)

    with pytest.raises(ClockError):
        probe_server_offset(Source(), "EURUSD", lambda: datetime(2024, 6, 3, 12),
                            samples=1, sleep_s=0.0)
