"""Regime and higher-timeframe bias tests (§7.3 steps 1-2)."""

from __future__ import annotations

import math
from datetime import timedelta

import pandas as pd
import pytest
from tests.conftest import load_bars

from fxbot.core.enums import Bias, Regime
from fxbot.data.resample import resample_h1_to_d1
from fxbot.strategy.regime import classify_regime, htf_bias


def daily(name: str, clock, cfg) -> pd.DataFrame:
    """Resample a golden fixture to broker-day D1, excluding the forming day."""
    frame = load_bars(name)
    now = frame.index[-1].to_pydatetime() + timedelta(minutes=cfg.timeframe_minutes)
    return resample_h1_to_d1(frame, clock, now)


def test_uptrend_gives_long_only(cfg, clock) -> None:
    """Price well above the daily EMA plus the band is LONG_ONLY."""
    assert htf_bias(daily("clean_long", clock, cfg), cfg.strategy) is Bias.LONG_ONLY


def test_downtrend_gives_short_only(cfg, clock) -> None:
    """Price well below the daily EMA minus the band is SHORT_ONLY."""
    assert htf_bias(daily("clean_short", clock, cfg), cfg.strategy) is Bias.SHORT_ONLY


def test_price_inside_the_band_is_neutral(cfg, clock) -> None:
    """The dead zone exists to stop the bias flipping daily around the EMA."""
    assert htf_bias(daily("bias_neutral", clock, cfg), cfg.strategy) is Bias.NEUTRAL


def test_too_little_daily_history_is_neutral_not_a_guess(cfg, clock) -> None:
    """Fail closed: an unwarmed daily EMA never yields a tradeable bias (§0.7)."""
    short = daily("clean_long", clock, cfg).iloc[:10]
    assert htf_bias(short, cfg.strategy) is Bias.NEUTRAL
    assert htf_bias(short.iloc[:0], cfg.strategy) is Bias.NEUTRAL


def test_resample_never_includes_the_forming_daily_bar(cfg, clock) -> None:
    """Using today's unclosed daily bar for bias is lookahead and a hard ban (§17.2)."""
    frame = load_bars("clean_long")
    now = frame.index[-1].to_pydatetime() + timedelta(minutes=cfg.timeframe_minutes)
    d1 = resample_h1_to_d1(frame, clock, now)
    assert d1.index[-1].date() < clock.trading_day(now)


def test_resample_aggregates_the_broker_day_correctly(cfg, clock) -> None:
    """Open of the first bar, high/low of the day, close of the last bar."""
    frame = load_bars("clean_long")
    now = frame.index[-1].to_pydatetime() + timedelta(minutes=cfg.timeframe_minutes)
    d1 = resample_h1_to_d1(frame, clock, now)
    day = d1.index[5].date()
    slice_ = frame[[clock.trading_day(ts.to_pydatetime()) == day for ts in frame.index]]
    assert d1["open"].iloc[5] == pytest.approx(slice_["open"].iloc[0])
    assert d1["high"].iloc[5] == pytest.approx(slice_["high"].max())
    assert d1["low"].iloc[5] == pytest.approx(slice_["low"].min())
    assert d1["close"].iloc[5] == pytest.approx(slice_["close"].iloc[-1])


def test_trending_regime_on_the_clean_case(cfg) -> None:
    """A real trend with mid-band volatility classifies as TRENDING."""
    regime, adx, rank = classify_regime(load_bars("clean_long"), cfg.strategy)
    assert regime is Regime.TRENDING
    assert adx >= cfg.strategy.adx_min
    assert cfg.strategy.atr_pct_floor <= rank <= cfg.strategy.atr_pct_ceiling


def test_low_adx_is_ranging(cfg) -> None:
    """Chop is RANGING even when volatility sits in the normal band."""
    regime, adx, rank = classify_regime(load_bars("adx_blocked"), cfg.strategy)
    assert regime is Regime.RANGING
    assert adx < cfg.strategy.adx_min
    assert cfg.strategy.atr_pct_floor <= rank <= cfg.strategy.atr_pct_ceiling


def test_volatility_ceiling_is_extreme(cfg) -> None:
    """A blowout is EXTREME, and the ceiling is checked before the ADX floor."""
    regime, _, rank = classify_regime(load_bars("vol_ceiling"), cfg.strategy)
    assert regime is Regime.EXTREME
    assert rank > cfg.strategy.atr_pct_ceiling


def test_unfilled_percentile_window_is_ranging_with_a_nan_rank(cfg) -> None:
    """A NaN rank must never read as tradeable; it classifies RANGING and rejects."""
    frame = load_bars("clean_long").iloc[-100:]
    regime, _, rank = classify_regime(frame, cfg.strategy)
    assert regime is Regime.RANGING
    assert math.isnan(rank)


def test_the_percentile_window_excludes_the_signal_bar(cfg) -> None:
    """Including the current bar makes the rank self-referential (§18 resolution)."""
    import numpy as np

    from fxbot.indicators.atr import atr
    from fxbot.indicators.stats import percentile_rank

    frame = load_bars("clean_long")
    p = cfg.strategy
    high, low, close = (frame[c].to_numpy(float) for c in ("high", "low", "close"))
    series = atr(high, low, close, p.atr_period) / close
    expected = percentile_rank(float(series[-1]),
                               series[-(p.atr_pct_window + 1):-1])
    _, _, rank = classify_regime(frame, p)
    assert rank == pytest.approx(expected)
    assert not np.isnan(expected)
