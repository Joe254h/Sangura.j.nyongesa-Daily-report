# fxbot

A trend-following forex bot for **MetaTrader 5**, trading FX majors on H1 through a
Pepperstone Razor account.

Built to the binding specification in `FXBOT_MASTER_PROMPT.md`. Section references
throughout the code (`§8.2`, `§12.5`, …) point back at it.

> **This is engineering, not a claim of profitability.** Nothing here implies the strategy
> makes money — the burden of proof is the walk-forward gate in §11.4, and `fxbot
> walkforward` reports a pass or a failure honestly. Confirm the tax and regulatory
> treatment of automated FX trading in Kenya independently.

---

## The one idea

Signal generation and position management exist in **exactly one implementation**
(`src/fxbot/strategy/`), which is pure: bars in, intents out. The backtester and the live
engine are thin adapters that feed it bars and execute its intents.

```
LIVE:      MT5 bars ──▶ StrategyContext ──▶ generate_signal() ──▶ Intent ──▶ RiskGovernor ──▶ MT5Broker
BACKTEST:  CSV bars ──▶ StrategyContext ──▶ generate_signal() ──▶ Intent ──▶ RiskGovernor ──▶ PaperBroker
                                            ▲ same function      ▲ same object
```

`tests/test_parity.py` runs 2,000 bars through both and asserts identical trade counts,
entry bars, sides and exit reasons, with prices agreeing to **1e-9**. If they ever
disagree, the build fails. That test is why the architecture is shaped this way.

## Quick start (research, any OS)

`MetaTrader5` ships Windows wheels only, so it is an optional dependency guarded by a
platform marker. Everything except live trading runs on Linux and macOS.

```bash
python -m pip install -e ".[dev]"
python -m pytest -q                      # 347 tests, including the parity keystone
```

## Live trading (Windows VPS)

```powershell
.\deploy\bootstrap_vps.ps1 -RepoUrl <url> -RdpAllowFrom <your.static.ip>
python -m scripts.download_history --env demo --years 8 --dump-specs
.\deploy\install_service.ps1 -Env demo
```

Then read `deploy/RUNBOOK.md` before you need it, not after.

## Commands

```
fxbot status      --env live              # risk status, equity, open tickets, reject histogram
fxbot kill        --env live              # halt: no new orders; open stops stay on the broker
fxbot reset       --env live --operator X # the only way out of HALTED
fxbot flatten     --env live --yes        # close every bot position at market
fxbot download    --env demo --dump-specs # 8 years of H1 to parquet, plus live symbol specs
fxbot backtest    --env backtest          # one run, full report and equity curve CSV
fxbot walkforward --env backtest          # the §11.4 protocol and the go-live gate table
fxbot live        --env live              # the loop the service runs
```

## Layout

```
src/fxbot/
  core/         frozen models, enums, errors, the broker clock      PURE
  indicators/   EMA, Wilder ATR/ADX, Donchian, percentile rank      PURE
  config/       one pydantic tree, loaded once, frozen
  strategy/     THE strategy and THE exit logic                     PURE
  risk/         sizing, exposure, the governor and the kill switch
  data/         MT5 source, parquet cache, quality gates, resample
  execution/    the Broker port, MT5Broker, PaperBroker, filling, retries
  runtime/      the engine, the scheduler, the SQLite journal
  backtest/     Backtrader adapter, the shared fill model, metrics, walk-forward
  ops/          structured logging with redaction, alerts, heartbeat
```

Dependencies point inward only, and `tests/test_layering.py` parses every module's AST to
prove it. `backtrader` may not be imported outside `backtest/` — including transitively
into the live engine.

## The rules that shaped this

Nine of them override everything else. In short:

1. **One brain, two bodies.** A trading rule written twice is a bug.
2. **Purity boundary.** `core/`, `indicators/`, `strategy/` and `risk/sizing.py` do no I/O.
3. **Closed bars only.** Never read the forming bar.
4. **Risk is checked before every order.** `RiskGovernor.approve()` is the only path to the
   broker.
5. **The kill switch survives restarts.** Risk state is persisted atomically and reloaded.
6. **Server time, never local time.** Day boundaries come from broker tick timestamps.
7. **Fail closed.** Any uncertainty halts new entries.
8. **Money-touching code needs a test first.**
9. **Never optimise on the out-of-sample set.**

## Known modelling limits, stated plainly

* A short position's stop is a buy-stop that really triggers on the **ask**, but historical
  bars carry only bid extremes, so the trigger is tested against the bar high. The fill
  still pays the full spread. The trigger is one spread late for shorts — about 0.1 pip on
  Razor EURUSD. The 1.5× spread stress run is where it shows.
* Swap is modelled for `SYMBOL_SWAP_MODE_POINTS` only. A symbol quoting swaps another way
  makes the runner **refuse to report** rather than silently drop the cost.
* There is no server-side take-profit in v1: the 1.5R exit is a partial close and the
  runner is trailed, and a static TP can express neither. A VPS outage therefore leaves the
  runner protected by its stop only.

## Two things the spec said that turned out to be wrong

Both are documented at the point of the fix, with the reasoning:

* **`warmup_bars = 600` cannot warm the daily bias gate.** 600 H1 bars is about 25 broker
  days; `htf_bias` needs 50 daily bars for its EMA. At 600 the bias is permanently
  `NEUTRAL` and the bot never trades — a failure that reads as "no edge" rather than "the
  window is too short". `StrategyParams.context_bars` now fetches enough for both
  timeframes; `warmup_bars` keeps its documented meaning.
* **`backtest/` cannot host the replay runner.** §12.5 wants the live engine driven over
  fixture bars, but §2.1 forbids `backtest/` from importing `runtime.engine`.
  `replay_history` therefore lives in `runtime/engine.py`. `tests/test_layering.py` caught
  the arrow pointing the wrong way.
