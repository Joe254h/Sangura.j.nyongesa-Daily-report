"""Configuration loading and schema tests.

**Why this file exists (§3 note).** §3's test list has no home for the config layer, but
§12.1 sets an 80% floor on everything outside ``risk``/``strategy``/``execution`` and
``config/loader.py`` is a substantial module. Burying its tests inside ``test_engine.py``
would hide them; a file named after the thing it tests does not.

The behaviour that matters most here: **an unrecognised YAML key is fatal**. That is how a
typo silently disables a risk limit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fxbot.config.loader import (
    deep_merge,
    default_config_dir,
    env_overrides,
    load_config,
    load_secrets,
    redacted,
)
from fxbot.config.schema import AppConfig, CostParams, ExecutionParams, RiskParams, StrategyParams
from fxbot.core.errors import ConfigError

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def write_config(directory: Path, base: dict, layer: dict, name: str = "demo") -> Path:
    """Write a minimal ``base.yaml`` plus one environment layer."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "base.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    (directory / f"{name}.yaml").write_text(yaml.safe_dump(layer), encoding="utf-8")
    return directory


def test_the_shipped_config_loads_for_every_environment() -> None:
    """Milestone 1's done-when: ``load_config`` validates for demo, live and backtest."""
    for env in ("demo", "live", "backtest"):
        cfg = load_config(env, CONFIG_DIR, environ={})
        assert cfg.env == env
        assert cfg.symbols


def test_live_starts_at_the_promotion_risk(cfg) -> None:
    """§13.5 step 4: the first four weeks of live run at 0.1% per trade."""
    live = load_config("live", CONFIG_DIR, environ={})
    assert live.risk.risk_per_trade_pct == pytest.approx(0.1)
    assert load_config("demo", CONFIG_DIR, environ={}).risk.risk_per_trade_pct == \
        pytest.approx(0.5)


def test_an_unrecognised_key_is_fatal(tmp_path: Path) -> None:
    """A typo must not be a warning: that is how a risk limit silently disappears (§5)."""
    directory = write_config(tmp_path, {"symbols": ["EURUSD"], "risk": {"clusters": {}}},
                             {"risk": {"risk_per_trade_pc": 5.0}})
    with pytest.raises(ConfigError) as exc:
        load_config("demo", directory, environ={})
    assert "risk_per_trade_pc" in str(exc.value)


def test_a_missing_file_is_fatal(tmp_path: Path) -> None:
    """No partial success: a configuration that will not load stops the process."""
    with pytest.raises(ConfigError, match="not found"):
        load_config("demo", tmp_path, environ={})


def test_malformed_yaml_is_fatal(tmp_path: Path) -> None:
    """Unparseable config halts rather than falling back to defaults (§0.7)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "base.yaml").write_text("symbols: [EURUSD\n", encoding="utf-8")
    (tmp_path / "demo.yaml").write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config("demo", tmp_path, environ={})

    (tmp_path / "base.yaml").write_text("- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config("demo", tmp_path, environ={})


def test_layers_merge_base_then_env_then_environment(tmp_path: Path) -> None:
    """``base.yaml`` <- ``{env}.yaml`` <- environment overrides, in that order."""
    directory = write_config(
        tmp_path,
        {"symbols": ["EURUSD"],
         "risk": {"risk_per_trade_pct": 0.5, "max_open_positions": 3, "clusters": {}}},
        {"risk": {"risk_per_trade_pct": 0.25}},
    )
    cfg = load_config("demo", directory, environ={})
    assert cfg.risk.risk_per_trade_pct == pytest.approx(0.25)
    assert cfg.risk.max_open_positions == 3

    override = load_config("demo", directory,
                           environ={"FXBOT__RISK__RISK_PER_TRADE_PCT": "0.1"})
    assert override.risk.risk_per_trade_pct == pytest.approx(0.1)


def test_deep_merge_replaces_lists_wholesale() -> None:
    """Appending to ``trade_hours_server`` would be a surprising way to widen a session."""
    merged = deep_merge({"a": {"b": 1, "c": 2}, "hours": [1, 2, 3]},
                        {"a": {"c": 9}, "hours": [7]})
    assert merged == {"a": {"b": 1, "c": 9}, "hours": [7]}


def test_environment_overrides_are_parsed_as_yaml_scalars() -> None:
    """``"true"`` becomes a bool and ``"0.1"`` a float, not strings."""
    parsed = env_overrides({
        "FXBOT__EXECUTION__DRY_RUN": "true",
        "FXBOT__RISK__RISK_PER_TRADE_PCT": "0.1",
        "FXBOT__RISK__MAX_OPEN_POSITIONS": "2",
        "IGNORED": "x",
    })
    assert parsed == {"execution": {"dry_run": True},
                      "risk": {"risk_per_trade_pct": 0.1, "max_open_positions": 2}}
    assert env_overrides({}) == {}


def test_a_malformed_environment_override_is_fatal() -> None:
    """``FXBOT__`` with nothing after it is a mistake, not an empty override."""
    with pytest.raises(ConfigError):
        env_overrides({"FXBOT__": "1"})
    with pytest.raises(ConfigError):
        env_overrides({"FXBOT__RISK": "1", "FXBOT__RISK__MAX_OPEN_POSITIONS": "2"})


def test_secrets_come_only_from_the_environment() -> None:
    """Never in YAML, never in the repo (§13.6)."""
    found = load_secrets({"MT5_LOGIN": "123", "MT5_PASSWORD": "x", "UNRELATED": "y"})
    assert found == {"MT5_LOGIN": "123", "MT5_PASSWORD": "x"}
    assert load_secrets({}) == {}


def test_the_logged_config_is_redacted(cfg: AppConfig) -> None:
    """The resolved config is logged at startup; a secret must not debut in a log file."""
    dumped = redacted(cfg)
    assert dumped["env"] == "backtest"
    assert isinstance(dumped["paths"]["data_dir"], str)
    assert "password" not in str(dumped).lower() or "REDACTED" in str(dumped)


def test_default_config_dir_is_overridable(monkeypatch) -> None:  # noqa: ANN001
    """The Windows service points ``FXBOT_CONFIG_DIR`` at ``C:\\fxbot\\config``."""
    monkeypatch.setenv("FXBOT_CONFIG_DIR", "/tmp/somewhere")
    assert default_config_dir() == Path("/tmp/somewhere")
    monkeypatch.delenv("FXBOT_CONFIG_DIR")
    assert default_config_dir().name == "config"


# ---------------------------------------------------------------- schema invariants


def test_incoherent_strategy_parameters_are_rejected() -> None:
    """A configuration that cannot produce a signal is a config error, not a quiet no-op."""
    with pytest.raises(ValueError, match="strictly faster"):
        StrategyParams(ema_fast=50, ema_slow=20)
    with pytest.raises(ValueError, match="atr_pct_floor"):
        StrategyParams(atr_pct_floor=0.9, atr_pct_ceiling=0.2)
    with pytest.raises(ValueError, match="periods"):
        StrategyParams(atr_period=1)
    with pytest.raises(ValueError, match="tp1_fraction"):
        StrategyParams(tp1_fraction=1.5)
    with pytest.raises(ValueError, match="ATR multiples"):
        StrategyParams(sl_atr_mult=0.0)
    with pytest.raises(ValueError, match="warmup_bars"):
        StrategyParams(warmup_bars=100)


def test_incoherent_risk_parameters_are_rejected() -> None:
    """The reduced threshold must trip before the lockout, or REDUCED is unreachable."""
    with pytest.raises(ValueError, match="reduced_after_consecutive_losses"):
        RiskParams(reduced_after_consecutive_losses=5, max_consecutive_losses=5)
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        RiskParams(risk_per_trade_pct=0.0)
    with pytest.raises(ValueError, match="total_open_risk_pct"):
        RiskParams(risk_per_trade_pct=2.0, total_open_risk_pct=1.0)
    with pytest.raises(ValueError, match="reduced_risk_multiplier"):
        RiskParams(reduced_risk_multiplier=1.5)


def test_execution_and_cost_tables_need_a_default() -> None:
    """A per-symbol table with no DEFAULT would raise KeyError on an unlisted symbol."""
    with pytest.raises(ValueError, match="DEFAULT"):
        ExecutionParams(max_spread_points={"EURUSD": 15})
    with pytest.raises(ValueError, match="DEFAULT"):
        CostParams(slippage_points={"EURUSD": 3})
    with pytest.raises(ValueError, match="comment"):
        ExecutionParams(order_comment_prefix="a-very-long-prefix")


def test_per_symbol_lookups_fall_back_to_default() -> None:
    """An unconfigured symbol gets the DEFAULT, never a crash."""
    execution = ExecutionParams(max_spread_points={"DEFAULT": 25, "EURUSD": 15})
    assert execution.spread_cap("EURUSD") == 15
    assert execution.spread_cap("XAUUSD") == 25
    costs = CostParams()
    assert costs.slippage("XAUUSD") == costs.slippage_points["DEFAULT"]
    assert costs.fixed_spread("XAUUSD") == costs.fixed_spread_points["DEFAULT"]


def test_clusters_must_name_symbols_in_the_universe() -> None:
    """A cluster naming a symbol you do not trade silently caps nothing."""
    with pytest.raises(ValueError, match="not in the universe"):
        AppConfig(env="demo", symbols=["EURUSD"],
                  risk=RiskParams(clusters={"BLOC": ["EURUSD", "NZDUSD"]}))
    with pytest.raises(ValueError, match="duplicates"):
        AppConfig(env="demo", symbols=["EURUSD", "EURUSD"], risk=RiskParams(clusters={}))
    with pytest.raises(ValueError, match="must not be empty"):
        AppConfig(env="demo", symbols=[], risk=RiskParams(clusters={}))


def test_session_hours_are_validated_and_normalised() -> None:
    """Duplicates collapse and out-of-range hours are refused."""
    from fxbot.config.schema import SessionParams

    assert SessionParams(trade_hours_server=[9, 8, 8, 7]).trade_hours_server == [7, 8, 9]
    with pytest.raises(ValueError, match=r"\[0, 23\]"):
        SessionParams(trade_hours_server=[24])
    with pytest.raises(ValueError, match="not be empty"):
        SessionParams(trade_hours_server=[])


def test_config_is_frozen(cfg: AppConfig) -> None:
    """Loaded once, frozen, injected downward: nothing mutates it mid-run (§5)."""
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic raises its own type
        cfg.risk.risk_per_trade_pct = 5.0  # type: ignore[misc]


def test_derived_paths_and_helpers(cfg: AppConfig) -> None:
    """The path helpers are what every module uses instead of joining strings."""
    assert cfg.paths.risk_state_path.name == "risk_state.json"
    assert cfg.paths.filling_cache_path.name == "filling_modes.json"
    assert cfg.paths.journal_path.name == "journal.db"
    assert cfg.timeframe_minutes == 60
    assert cfg.costs.commission_per_lot_round_turn == pytest.approx(7.0)


# ---------------------------------------------------------------- the command line

def run_cli(args: list[str], cwd: Path, config_dir: Path = CONFIG_DIR):  # noqa: ANN201
    """Invoke the CLI through Typer's test runner, isolated to ``cwd``."""
    import os

    from typer.testing import CliRunner

    from fxbot.cli import app

    previous = Path.cwd()
    os.environ["FXBOT_CONFIG_DIR"] = str(config_dir)
    os.chdir(cwd)
    try:
        return CliRunner().invoke(app, args)
    finally:
        os.chdir(previous)


def test_status_reports_a_fresh_state(tmp_path: Path) -> None:
    """`fxbot status` is the first thing the runbook tells you to run."""
    result = run_cli(["status", "--env", "backtest"], tmp_path)
    assert result.exit_code == 0, result.output
    assert "risk status       NORMAL" in result.output
    assert "open tickets      []" in result.output


def test_kill_halts_without_closing_anything(tmp_path: Path) -> None:
    """Halting stops new orders; open trades keep their broker-side stops (§8.5)."""
    assert run_cli(["kill", "--env", "backtest", "--reason", "smoke"],
                   tmp_path).exit_code == 0
    status = run_cli(["status", "--env", "backtest"], tmp_path)
    assert "HALTED" in status.output
    assert "smoke" in status.output


def test_reset_requires_an_operator_and_clears_the_halt(tmp_path: Path) -> None:
    """The operator name is the audit trail; the CLI will not proceed without it."""
    run_cli(["kill", "--env", "backtest", "--reason", "smoke"], tmp_path)
    missing = run_cli(["reset", "--env", "backtest"], tmp_path)
    assert missing.exit_code != 0

    done = run_cli(["reset", "--env", "backtest", "--operator", "sangura"], tmp_path)
    assert done.exit_code == 0
    assert "sangura" in done.output
    assert "NORMAL" in run_cli(["status", "--env", "backtest"], tmp_path).output


def test_reset_refuses_when_nothing_is_halted(tmp_path: Path) -> None:
    """A reset that resets nothing exits non-zero rather than pretending it worked."""
    result = run_cli(["reset", "--env", "backtest", "--operator", "sangura"], tmp_path)
    assert result.exit_code == 1
    assert "not halted" in result.output


def test_flatten_refuses_without_confirmation(tmp_path: Path) -> None:
    """It closes real positions, so it needs `--yes`."""
    result = run_cli(["flatten", "--env", "backtest"], tmp_path)
    assert result.exit_code == 1
    assert "refusing" in result.output


def test_backtest_refuses_without_cached_history(tmp_path: Path) -> None:
    """Fail closed: a backtest missing a symbol would misreport 'profitable symbols'."""
    result = run_cli(["backtest", "--env", "backtest", "--symbol", "EURUSD"], tmp_path)
    assert result.exit_code == 1
    assert "no cached history" in result.output


def test_backtest_runs_both_engines_over_cached_history(tmp_path: Path) -> None:
    """End to end through the CLI: cache -> run -> report -> equity curve CSV."""
    from tests.conftest import load_bars

    from fxbot.data.cache import write_cache

    write_cache(tmp_path / "data", "EURUSD", "H1", load_bars("parity_eurusd").iloc[:900])
    for engine in ("backtrader", "replay"):
        result = run_cli(["backtest", "--env", "backtest", "--symbol", "EURUSD",
                          "--engine", engine, "--out", str(tmp_path / "reports")], tmp_path)
        assert result.exit_code == 0, result.output
        assert "net profit" in result.output
        assert "reject reasons" in result.output
        assert (tmp_path / "reports" / f"backtest_{engine}.txt").is_file()
        assert (tmp_path / "reports" / f"backtest_{engine}_equity.csv").is_file()


def test_walkforward_prints_the_gate_table(tmp_path: Path) -> None:
    """A short history cannot pass §11.4, and the CLI has to say so plainly."""
    from tests.conftest import load_bars

    from fxbot.data.cache import write_cache

    for symbol, fixture in (("EURUSD", "parity_eurusd"), ("GBPUSD", "parity_gbpusd")):
        write_cache(tmp_path / "data", symbol, "H1", load_bars(fixture).iloc[:900])
    result = run_cli(["walkforward", "--env", "backtest", "--out", str(tmp_path / "reports")],
                     tmp_path)
    assert result.exit_code == 0, result.output
    assert "DO NOT GO LIVE" in result.output
    assert "not to re-optimise" in result.output
    assert (tmp_path / "reports" / "walkforward.txt").is_file()


def test_walkforward_refuses_without_history(tmp_path: Path) -> None:
    """Failure path: nothing cached, nothing to optimise."""
    result = run_cli(["walkforward", "--env", "backtest"], tmp_path)
    assert result.exit_code == 1
    assert "no cached history" in result.output


def test_the_coverage_gate_reads_the_report(tmp_path: Path) -> None:
    """§12.1's four floors cannot be expressed by --cov-fail-under; this is what checks them."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.check_coverage import floor_for, main

    assert floor_for("fxbot/risk/governor.py") == ("fxbot/risk", 100.0)
    assert floor_for("fxbot/strategy/manage.py") == ("fxbot/strategy", 95.0)
    assert floor_for("fxbot/execution/mt5_broker.py") == ("fxbot/execution", 90.0)
    assert floor_for("fxbot/ops/alerts.py") == ("", 80.0)
    assert main(["check_coverage.py", str(tmp_path / "missing.xml")]) == 2

    report = tmp_path / "coverage.xml"
    report.write_text(
        '<coverage><packages><package><classes>'
        '<class filename="src/fxbot/risk/sizing.py"><lines>'
        '<line number="1" hits="1"/><line number="2" hits="0"/>'
        "</lines></class></classes></package></packages></coverage>",
        encoding="utf-8")
    assert main(["check_coverage.py", str(report)]) == 1
