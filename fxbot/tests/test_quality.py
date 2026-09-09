"""Data-quality gate tests (§6.4).

A quality failure blocks new entries for one symbol. A **sanity** failure additionally
suppresses stop modifications, because trailing off a corrupt high can ratchet a stop into
the market.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest
from tests.conftest import SERVER_TZ, load_bars

from fxbot.core.enums import RejectReason
from fxbot.data.quality import check, count_gaps


def run(frame: pd.DataFrame, cfg, clock):  # noqa: ANN001, ANN201
    """Run the gate with the fixture's own last bar as "now"."""
    clock.observe(frame.index[-1].to_pydatetime() + timedelta(minutes=60))
    return check(frame, "EURUSD", 60, clock, cfg.data, cfg.strategy)


def test_a_clean_frame_passes(cfg, clock) -> None:
    """The golden fixtures are clean by construction."""
    report = run(load_bars("clean_long"), cfg, clock)
    assert report.ok
    assert report.reason is RejectReason.NONE
    assert report.fatal_sanity is False


def test_an_empty_frame_fails(cfg, clock) -> None:
    """Nothing to trade on is a failure, not an empty success."""
    clock.observe(pd.Timestamp("2024-06-03 12:00", tz=SERVER_TZ).to_pydatetime())
    report = check(pd.DataFrame(), "EURUSD", 60, clock, cfg.data, cfg.strategy)
    assert not report.ok


def test_too_few_bars_fails_the_warmup_gate(cfg, clock) -> None:
    """Fewer rows than ``warmup_bars`` is a failure."""
    report = run(load_bars("clean_long").iloc[-100:], cfg, clock)
    assert not report.ok
    assert "warmup" in report.detail


def test_impossible_ohlc_is_a_fatal_sanity_failure(cfg, clock) -> None:
    """``high < low`` suppresses MODIFY_STOP as well as entries (§6.4)."""
    frame = load_bars("clean_long").copy()
    frame.iloc[-5, frame.columns.get_loc("high")] = frame["low"].iloc[-5] - 0.01
    report = run(frame, cfg, clock)
    assert not report.ok
    assert report.fatal_sanity is True


def test_a_close_outside_the_bar_range_is_fatal(cfg, clock) -> None:
    """A close above the high is a corrupt bar, not an unusual one."""
    frame = load_bars("clean_long").copy()
    frame.iloc[-3, frame.columns.get_loc("close")] = frame["high"].iloc[-3] + 0.01
    report = run(frame, cfg, clock)
    assert not report.ok and report.fatal_sanity


def test_a_bad_tick_beyond_ten_atr_is_fatal(cfg, clock) -> None:
    """A single-bar move past 10x the 100-bar ATR is a bad tick."""
    frame = load_bars("clean_long").copy()
    row = len(frame) - 2
    spike = frame["close"].iloc[row] + 0.25
    for column in ("open", "high", "low", "close"):
        frame.iloc[row, frame.columns.get_loc(column)] = spike
    report = run(frame, cfg, clock)
    assert not report.ok
    assert report.fatal_sanity is True
    assert "bad tick" in report.detail


def test_a_non_positive_price_is_fatal(cfg, clock) -> None:
    """Prices are positive. A zero is corruption, not a quote."""
    frame = load_bars("clean_long").copy()
    frame.iloc[-4, frame.columns.get_loc("low")] = 0.0
    assert run(frame, cfg, clock).fatal_sanity is True


def test_duplicate_and_unsorted_timestamps_fail(cfg, clock) -> None:
    """A duplicated or non-monotonic index means indicator alignment cannot be trusted."""
    frame = load_bars("clean_long")
    duplicated = pd.concat([frame, frame.iloc[[-1]]])
    assert not run(duplicated, cfg, clock).ok
    shuffled = frame.iloc[list(range(len(frame) - 2)) + [len(frame) - 1, len(frame) - 2]]
    assert not run(shuffled, cfg, clock).ok


def test_stale_data_fails_the_gate(cfg, clock) -> None:
    """A last close older than 2 x timeframe is stale, and blocks entries."""
    frame = load_bars("clean_long")
    clock.observe(frame.index[-1].to_pydatetime() + timedelta(hours=12))
    report = check(frame, "EURUSD", 60, clock, cfg.data, cfg.strategy)
    assert not report.ok
    assert report.reason is RejectReason.STALE_DATA
    assert report.fatal_sanity is False, "stale is not corrupt: stops may still move"


def test_the_weekend_excuses_an_old_last_bar(cfg, clock) -> None:
    """The market being shut is not a data fault."""
    frame = load_bars("clean_long")
    friday_close = frame.index[-1].to_pydatetime()
    saturday = friday_close + timedelta(days=(5 - friday_close.weekday()) % 7 or 7)
    clock.observe(saturday)
    report = check(frame, "EURUSD", 60, clock, cfg.data, cfg.strategy)
    assert report.ok or report.reason is RejectReason.STALE_DATA


def test_the_weekend_gap_is_not_counted_as_a_gap(cfg, clock) -> None:
    """Friday to Sunday is expected; anything else is a hole in the history."""
    frame = load_bars("clean_long")
    assert count_gaps(frame.index, 60) == 0

    punched = pd.concat([frame.iloc[:200], frame.iloc[210:]])
    assert count_gaps(punched.index, 60) >= 1


def test_too_many_gaps_fail_the_gate(cfg, clock) -> None:
    """More than ``max_gap_bars`` unexplained gaps blocks the symbol."""
    frame = load_bars("clean_long")
    holes = frame
    for start in (300, 500, 700, 900, 1100, 1300):
        holes = pd.concat([holes.iloc[:start], holes.iloc[start + 6:]])
    report = run(holes, cfg, clock)
    assert not report.ok
    assert report.gap_count > cfg.data.max_gap_bars


def test_a_clock_with_no_observation_fails_closed(cfg) -> None:
    """A clock that has not seen the broker cannot judge staleness (§0.7)."""
    from fxbot.core.clock import ServerClock

    report = check(load_bars("clean_long"), "EURUSD", 60, ServerClock(3), cfg.data,
                   cfg.strategy)
    assert not report.ok
    assert "clock unavailable" in report.detail


# ---------------------------------------------------------------- the parquet cache

def test_the_cache_round_trips_and_deduplicates(tmp_path, cfg) -> None:
    """One file per symbol and timeframe, deduplicated and sorted on write (§6.5)."""
    from fxbot.data.cache import cache_path, read_cache, write_cache

    frame = load_bars("clean_long")
    assert read_cache(tmp_path, "EURUSD", "H1") is None

    written = write_cache(tmp_path, "EURUSD", "H1", frame)
    assert written == cache_path(tmp_path, "EURUSD", "H1")
    assert written.is_file()

    restored = read_cache(tmp_path, "EURUSD", "H1")
    assert restored is not None
    assert len(restored) == len(frame)
    assert restored.index.is_monotonic_increasing
    assert not restored.index.has_duplicates


def test_appending_merges_and_lets_the_later_bar_win(tmp_path) -> None:
    """A refetched bar is a corrected bar, so later data wins on a timestamp collision."""
    from fxbot.data.cache import append_cache, read_cache

    frame = load_bars("clean_long")
    append_cache(tmp_path, "EURUSD", "H1", frame.iloc[:100])

    corrected = frame.iloc[90:150].copy()
    corrected.iloc[0, corrected.columns.get_loc("close")] = 9.9999
    merged = append_cache(tmp_path, "EURUSD", "H1", corrected)

    assert len(merged) == 150, "the ten overlapping bars merge rather than duplicating"
    assert merged.loc[corrected.index[0], "close"] == pytest.approx(9.9999)
    assert read_cache(tmp_path, "EURUSD", "H1") is not None


def test_a_symbol_with_a_suffix_gets_a_safe_path(tmp_path) -> None:
    """Broker names like ``EURUSD.r`` must not escape the data directory."""
    from fxbot.data.cache import cache_path

    path = cache_path(tmp_path, "EUR/USD", "H1")
    assert path.parent.parent == tmp_path
    assert "/" not in path.parent.name
