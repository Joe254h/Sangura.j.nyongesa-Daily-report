"""Configuration loading and secret handling (§5).

``load_config(env)`` deep-merges ``base.yaml`` <- ``{env}.yaml`` <- environment overrides,
validates the result, and returns a frozen :class:`~fxbot.config.schema.AppConfig`.

An unrecognised YAML key is a **fatal** error, not a warning: every model in
``config/schema.py`` sets ``extra="forbid"``. That is how a typo such as
``risk_per_trade_pc`` silently disables a risk limit.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from fxbot.config.schema import AppConfig
from fxbot.core.errors import ConfigError

ENV_PREFIX = "FXBOT__"
"""Prefix for environment overrides. ``FXBOT__RISK__RISK_PER_TRADE_PCT=0.1``."""

SECRET_NAMES = (
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "MT5_TERMINAL_PATH",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)
"""Secrets live in the environment only -- never in YAML, never in the repo (§13.6)."""

_REDACT_KEYS = frozenset({"password", "token", "secret", "login", "chat_id"})


def default_config_dir() -> Path:
    """Return the directory holding the YAML configuration files.

    ``FXBOT_CONFIG_DIR`` wins when set (that is how the service points at
    ``C:\\fxbot\\config``); otherwise the ``config/`` directory beside ``src/``.
    """
    override = os.environ.get("FXBOT_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "config"


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read one YAML file into a dict, or raise :class:`ConfigError`."""
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return loaded


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` without mutating either.

    Nested mappings merge key by key; every other type (lists included) is replaced
    wholesale. Replacing lists is deliberate -- appending to ``trade_hours_server``
    from an environment file would be a surprising way to widen a session window.

    Args:
        base: The lower-priority mapping.
        override: The higher-priority mapping.

    Returns:
        A new merged dict.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _coerce_scalar(raw: str) -> Any:
    """Parse an environment-variable value as a YAML scalar (int, float, bool, str)."""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build a nested override mapping from ``FXBOT__``-prefixed environment variables.

    ``FXBOT__RISK__RISK_PER_TRADE_PCT=0.1`` becomes
    ``{"risk": {"risk_per_trade_pct": 0.1}}``.

    Args:
        environ: Environment mapping; defaults to :data:`os.environ`.

    Returns:
        A nested dict suitable for :func:`deep_merge`.
    """
    source = os.environ if environ is None else environ
    out: dict[str, Any] = {}
    for key, raw in source.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = [part.lower() for part in key[len(ENV_PREFIX):].split("__") if part]
        if not path:
            raise ConfigError(f"malformed environment override: {key}")
        cursor: MutableMapping[str, Any] = out
        for part in path[:-1]:
            nxt = cursor.setdefault(part, {})
            if not isinstance(nxt, dict):
                raise ConfigError(f"environment override {key} conflicts with an earlier one")
            cursor = nxt
        cursor[path[-1]] = _coerce_scalar(raw)
    return out


def load_config(
    env: str,
    config_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load, merge, validate and freeze the configuration for ``env``.

    Args:
        env: One of ``demo``, ``live`` or ``backtest``.
        config_dir: Directory holding the YAML files; defaults to
            :func:`default_config_dir`.
        environ: Environment mapping for overrides; defaults to :data:`os.environ`.

    Returns:
        The validated, frozen :class:`~fxbot.config.schema.AppConfig`.

    Raises:
        ConfigError: On a missing file, malformed YAML, an unrecognised key, or any
            validation failure. There is no partial success: a configuration that does not
            validate stops the process (§0.7).
    """
    directory = default_config_dir() if config_dir is None else config_dir
    base = _read_yaml(directory / "base.yaml")
    layer = _read_yaml(directory / f"{env}.yaml")
    merged = deep_merge(deep_merge(base, layer), env_overrides(environ))
    merged["env"] = env
    try:
        return AppConfig(**merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration for env={env!r}:\n{exc}") from exc


def load_secrets(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read the connection secrets from the environment.

    Args:
        environ: Environment mapping; defaults to :data:`os.environ`.

    Returns:
        A dict of the secret names that are set. Missing entries are simply absent --
        the caller that needs one (``MT5DataSource.connect``) raises with a precise
        message, which beats a generic "config incomplete" at startup.
    """
    source = os.environ if environ is None else environ
    return {name: source[name] for name in SECRET_NAMES if source.get(name)}


def redacted(config: AppConfig) -> dict[str, Any]:
    """Return the resolved configuration as a dict, with any secret-shaped value masked.

    This is what gets logged at startup (§5). The config tree holds no secrets by
    construction, but the redaction runs anyway: the day someone adds one, the log must
    not be where it first appears.

    Args:
        config: The resolved configuration.

    Returns:
        A JSON-serialisable dict.
    """

    def _walk(value: Any, key: str = "") -> Any:
        if any(token in key.lower() for token in _REDACT_KEYS):
            return "***REDACTED***"
        if isinstance(value, Mapping):
            return {k: _walk(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_walk(v, key) for v in value]
        if isinstance(value, Path):
            return str(value)
        return value

    return dict(_walk(config.model_dump(mode="json")))
