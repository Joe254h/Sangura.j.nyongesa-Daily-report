"""The configuration model tree (§5).

One pydantic v2 model tree, loaded once, frozen, injected downward. **No module reads
config globally.** Secrets come from environment variables only and never appear in YAML.

Every model sets ``extra="forbid"``: an unrecognised key is a fatal error, not a warning.
That is how a typo silently disables a risk limit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_STRICT = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class StrategyParams(BaseModel):
    """Parameters of the H1 Donchian trend strategy (§7)."""

    model_config = _STRICT

    ema_fast: int = 20
    ema_slow: int = 50
    donchian_period: int = 20
    atr_period: int = 14
    adx_period: int = 14
    adx_min: float = 20.0
    d1_ema: int = 50
    d1_neutral_band_atr: float = 0.25
    """Dead-zone around the D1 EMA, in D1 ATR units."""
    atr_pct_window: int = 500
    """Bars for the volatility percentile."""
    atr_pct_floor: float = 0.20
    """Below this rank the market is dead -> RANGING."""
    atr_pct_ceiling: float = 0.90
    """Above this rank the market is blowing out -> EXTREME."""
    sl_atr_mult: float = 2.0
    tp1_r: float = 1.5
    tp1_fraction: float = 0.5
    trail_atr_mult: float = 3.0
    breakeven_at_r: float = 1.0
    use_partials: bool = True
    warmup_bars: int = 600
    """H1 bars of indicator warmup. See :attr:`context_bars` for the window actually
    fetched -- the daily bias gate needs more history than this number implies."""

    @property
    def d1_warmup_bars(self) -> int:
        """H1 bars needed before the D1 bias gate produces anything but ``NEUTRAL``.

        **Ambiguity resolved (§18).** §5 sets ``warmup_bars = 600`` and comments it as
        "max(atr_pct_window, indicators) + margin" -- but 600 H1 bars is about 25 broker
        days, and :func:`~fxbot.strategy.regime.htf_bias` needs ``d1_ema`` (50) plus
        ``atr_period`` (14) **daily** bars. At 600 the daily EMA is never finite, the bias
        is permanently ``NEUTRAL``, and the bot never takes a trade -- a failure that
        would read as "the strategy has no edge" rather than "the window is too short".
        Two readings were possible: raise the ``warmup_bars`` default (changes a number
        §5 gives verbatim) or fetch a window wide enough for both timeframes. The second
        is chosen: ``warmup_bars`` keeps its documented meaning as the H1 indicator
        warmup and stays at 600, and this property is what the adapters actually fetch.
        """
        return (self.d1_ema + self.atr_period + 5) * 24

    @property
    def context_bars(self) -> int:
        """H1 bars to fetch for a context: enough to warm both timeframes."""
        return max(self.warmup_bars, self.d1_warmup_bars)

    @field_validator("ema_fast", "ema_slow", "donchian_period", "atr_period", "adx_period",
                     "d1_ema", "atr_pct_window", "warmup_bars")
    @classmethod
    def _positive_period(cls, v: int) -> int:
        if v < 2:
            raise ValueError("indicator periods must be >= 2")
        return v

    @field_validator("tp1_fraction")
    @classmethod
    def _fraction(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError("tp1_fraction must be in (0, 1]")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> StrategyParams:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be strictly faster than ema_slow")
        if not 0.0 <= self.atr_pct_floor < self.atr_pct_ceiling <= 1.0:
            raise ValueError("require 0 <= atr_pct_floor < atr_pct_ceiling <= 1")
        if self.sl_atr_mult <= 0 or self.trail_atr_mult <= 0:
            raise ValueError("ATR multiples must be positive")
        needed = max(self.atr_pct_window + self.atr_period,
                     2 * self.adx_period + 1,
                     self.ema_slow, self.donchian_period) + 20
        if self.warmup_bars < needed:
            raise ValueError(f"warmup_bars={self.warmup_bars} is below the {needed} bars the "
                             "configured indicators need before they produce a finite value")
        return self


class RiskParams(BaseModel):
    """Risk limits and the kill-switch thresholds (§8.1)."""

    model_config = _STRICT

    risk_per_trade_pct: float = 0.5
    """Of equity, never balance (§17.8)."""
    daily_loss_limit_pct: float = 3.0
    max_drawdown_pct: float = 10.0
    """From the equity high-water mark -> HALTED."""
    max_consecutive_losses: int = 5
    reduced_risk_multiplier: float = 0.5
    reduced_after_consecutive_losses: int = 3
    max_open_positions: int = 3
    max_positions_per_symbol: int = 1
    max_positions_per_cluster: int = 2
    total_open_risk_pct: float = 1.5
    max_margin_utilisation_pct: float = 20.0
    flatten_on_daily_lockout: bool = False
    """Default OFF, and know why before flipping it (§8.5)."""
    clusters: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "USD_LONG_BLOC": ["USDJPY", "USDCAD"],
            "USD_SHORT_BLOC": ["EURUSD", "GBPUSD", "AUDUSD"],
        }
    )

    @model_validator(mode="after")
    def _coherent(self) -> RiskParams:
        if not 0.0 < self.risk_per_trade_pct <= 5.0:
            raise ValueError("risk_per_trade_pct must be in (0, 5]")
        if self.reduced_after_consecutive_losses >= self.max_consecutive_losses:
            raise ValueError("reduced_after_consecutive_losses must trip before "
                             "max_consecutive_losses, otherwise REDUCED is unreachable")
        if self.total_open_risk_pct < self.risk_per_trade_pct:
            raise ValueError("total_open_risk_pct below risk_per_trade_pct blocks every trade")
        if not 0.0 < self.reduced_risk_multiplier <= 1.0:
            raise ValueError("reduced_risk_multiplier must be in (0, 1]")
        return self


class ExecutionParams(BaseModel):
    """Order-construction and retry parameters (§9)."""

    model_config = _STRICT

    magic: int = 990117
    deviation_points: int = 20
    max_spread_points: dict[str, int] = Field(default_factory=lambda: {"DEFAULT": 25})
    max_retries: int = 3
    retry_backoff_s: float = 1.5
    order_comment_prefix: str = "fxbot"
    dry_run: bool = False
    """True -> PaperBroker even in the live environment."""

    @model_validator(mode="after")
    def _has_default(self) -> ExecutionParams:
        if "DEFAULT" not in self.max_spread_points:
            raise ValueError("max_spread_points must contain a DEFAULT entry")
        if len(self.order_comment_prefix) > 12:
            raise ValueError("order_comment_prefix must leave room in MT5's 31-char comment")
        return self

    def spread_cap(self, symbol: str) -> int:
        """Return the maximum acceptable spread in points for ``symbol``.

        Args:
            symbol: Canonical symbol name.

        Returns:
            The per-symbol override if configured, else ``DEFAULT``.
        """
        return self.max_spread_points.get(symbol, self.max_spread_points["DEFAULT"])


class SessionParams(BaseModel):
    """Trading-hours gate, evaluated in server time (§7.3 step 3)."""

    model_config = _STRICT

    trade_hours_server: list[int] = Field(
        default_factory=lambda: [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    )
    skip_friday_after_hour: int = 19
    skip_hours_after_weekend_open: int = 2
    news_blackout_minutes: int = 0
    """0 = disabled in v1."""

    @field_validator("trade_hours_server")
    @classmethod
    def _hours(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("trade_hours_server must not be empty")
        if any(not 0 <= h <= 23 for h in v):
            raise ValueError("trade_hours_server entries must be in [0, 23]")
        return sorted(set(v))


class DataParams(BaseModel):
    """Data-quality gates and history depth (§6.4)."""

    model_config = _STRICT

    max_gap_bars: int = 3
    max_stale_multiples: float = 2.0
    history_years: int = 8


class RuntimeParams(BaseModel):
    """Loop scheduling and watchdog (§10.1)."""

    model_config = _STRICT

    post_close_delay_s: float = 5.0
    connect_retry_seconds: int = 300
    watchdog_multiples: float = 3.0


class PathParams(BaseModel):
    """On-disk locations. All are gitignored."""

    model_config = _STRICT

    data_dir: Path = Path("data")
    state_dir: Path = Path("state")
    log_dir: Path = Path("logs")
    risk_state_file: str = "risk_state.json"
    filling_cache_file: str = "filling_modes.json"
    journal_db: str = "journal.db"

    @property
    def risk_state_path(self) -> Path:
        """Full path to the persisted risk state."""
        return self.state_dir / self.risk_state_file

    @property
    def filling_cache_path(self) -> Path:
        """Full path to the negotiated filling-mode cache."""
        return self.state_dir / self.filling_cache_file

    @property
    def journal_path(self) -> Path:
        """Full path to the SQLite journal."""
        return self.state_dir / self.journal_db


class AlertParams(BaseModel):
    """Telegram / webhook alerting (§14.2)."""

    model_config = _STRICT

    enabled: bool = True
    min_severity: Literal["INFO", "WARNING", "CRITICAL"] = "WARNING"
    heartbeat_url: str | None = None
    digest_hour_server: int = 0
    digest_minute_server: int = 5


class CostParams(BaseModel):
    """The backtest / paper cost model (§11.2)."""

    model_config = _STRICT

    commission_per_lot_per_side: float = 3.50
    """Pepperstone Razor MT5: USD 3.50 per lot per side, USD 7.00 round turn."""
    slippage_points: dict[str, int] = Field(default_factory=lambda: {"DEFAULT": 3})
    spread_source: Literal["historical", "fixed"] = "historical"
    fixed_spread_points: dict[str, int] = Field(default_factory=lambda: {"DEFAULT": 8})

    @model_validator(mode="after")
    def _has_defaults(self) -> CostParams:
        for name, table in (("slippage_points", self.slippage_points),
                            ("fixed_spread_points", self.fixed_spread_points)):
            if "DEFAULT" not in table:
                raise ValueError(f"{name} must contain a DEFAULT entry")
        return self

    @property
    def commission_per_lot_round_turn(self) -> float:
        """Round-turn commission per lot -- the number sizing must use (§8.2 step 4)."""
        return 2.0 * self.commission_per_lot_per_side

    def slippage(self, symbol: str) -> int:
        """Return the modelled slippage in points for ``symbol``."""
        return self.slippage_points.get(symbol, self.slippage_points["DEFAULT"])

    def fixed_spread(self, symbol: str) -> int:
        """Return the fallback fixed spread in points for ``symbol``."""
        return self.fixed_spread_points.get(symbol, self.fixed_spread_points["DEFAULT"])


class AppConfig(BaseModel):
    """The whole resolved configuration."""

    model_config = _STRICT

    env: Literal["demo", "live", "backtest"]
    symbols: list[str] = Field(
        default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]
    )
    timeframe: Literal["H1"] = "H1"
    strategy: StrategyParams = StrategyParams()
    risk: RiskParams = RiskParams()
    execution: ExecutionParams = ExecutionParams()
    session: SessionParams = SessionParams()
    costs: CostParams = CostParams()
    data: DataParams = DataParams()
    runtime: RuntimeParams = RuntimeParams()
    paths: PathParams = PathParams()
    alerts: AlertParams = AlertParams()

    @property
    def timeframe_minutes(self) -> int:
        """Bar length of :attr:`timeframe` in minutes."""
        return 60

    @model_validator(mode="after")
    def _coherent(self) -> AppConfig:
        if not self.symbols:
            raise ValueError("symbols must not be empty")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols contains duplicates")
        known = set(self.symbols)
        for cluster, members in self.risk.clusters.items():
            unknown = [m for m in members if m not in known]
            if unknown:
                raise ValueError(
                    f"cluster {cluster!r} names symbols not in the universe: {unknown}")
        return self
