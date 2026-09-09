"""Bulk H1 history download and symbol-spec capture (§6.5, §12.2).

Run on the VPS, where a terminal exists::

    python -m scripts.download_history --env demo --years 8 --dump-specs

Target history is **>= 8 years of H1** so walk-forward has enough folds. MT5 returns only
bars within the terminal's *Max. bars in chart* setting -- raise it to Unlimited first, and
expect a fresh terminal to hold far less than the server has until the chart is loaded.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from fxbot.config.loader import load_config, load_secrets
from fxbot.core.clock import ServerClock
from fxbot.data.cache import append_cache, read_cache
from fxbot.data.clock_probe import probe_server_offset
from fxbot.data.mt5_source import TIMEFRAME_H1, MT5DataSource
from fxbot.ops.health import utc_now
from fxbot.ops.logging import configure_logging

SPEC_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "specs"


def download_history(cfg, years: int, dump_specs: bool) -> None:  # type: ignore[no-untyped-def]
    """Download ``years`` of H1 bars for every configured symbol into the parquet cache.

    Appends incrementally: the fetch starts at the last cached bar minus ``warmup_bars``,
    so an interrupted run costs one refetch of the warmup window rather than eight years.

    Args:
        cfg: The resolved configuration.
        years: How much history to request.
        dump_specs: Also write ``tests/fixtures/specs/{symbol}.json`` from the live broker.
    """
    configure_logging(cfg.paths.log_dir, cfg.env)
    secrets = load_secrets()
    clock = ServerClock(0)
    source = MT5DataSource(cfg, clock)
    source.connect(secrets)
    resolved = source.resolve_symbols(list(cfg.symbols))
    offset = probe_server_offset(source, next(iter(resolved.values())), utc_now)
    print(f"broker server offset: UTC{offset:+d}")

    source = MT5DataSource(cfg, ServerClock(offset))
    source.connect(secrets)
    source.resolve_symbols(list(cfg.symbols))
    clock = ServerClock(offset)
    clock.observe(clock.from_utc(utc_now()))
    cfg.paths.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.paths.state_dir / "server_offset.json").write_text(
        json.dumps({"offset_hours": offset}), encoding="utf-8")

    end = clock.now()
    for canonical, broker_name in resolved.items():
        spec = source.symbol_spec(broker_name)
        if dump_specs:
            SPEC_DIR.mkdir(parents=True, exist_ok=True)
            (SPEC_DIR / f"{canonical}.json").write_text(
                json.dumps(asdict(spec), indent=2, sort_keys=True), encoding="utf-8")
            print(f"{canonical}: spec captured -> {SPEC_DIR / f'{canonical}.json'}")

        cached = read_cache(cfg.paths.data_dir, canonical, cfg.timeframe)
        start = end - timedelta(days=int(365.25 * years))
        if cached is not None and len(cached):
            warm = timedelta(minutes=cfg.timeframe_minutes * cfg.strategy.warmup_bars)
            start = max(start, cached.index[-1].to_pydatetime() - warm)

        frame = source.bars_range(broker_name, TIMEFRAME_H1, start, end)
        if frame.empty:
            print(f"{canonical}: no bars returned for {start} .. {end}")
            continue
        merged = append_cache(cfg.paths.data_dir, canonical, cfg.timeframe, frame)
        print(f"{canonical}: +{len(frame)} bars, {len(merged)} cached, "
              f"{merged.index[0]} .. {merged.index[-1]}")
    source.shutdown()


def main() -> None:
    """Parse arguments and run the download."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="demo", choices=["demo", "live", "backtest"])
    parser.add_argument("--years", type=int, default=0)
    parser.add_argument("--dump-specs", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.env)
    download_history(cfg, args.years or cfg.data.history_years, args.dump_specs)


if __name__ == "__main__":
    main()
