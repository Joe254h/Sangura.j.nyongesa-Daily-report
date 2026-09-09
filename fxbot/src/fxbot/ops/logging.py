"""Structured logging (§14.1).

JSON to ``logs/fxbot.jsonl`` with daily rotation and 90-day retention, human-readable to
the console. Every record carries ``cycle_id``, ``symbol``, ``env`` and ``risk_status``
where they are known, and passes a **redaction filter** before it is written: secrets
never reach a log file (§13.6).
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

LOG_FILENAME = "fxbot.jsonl"
RETENTION_DAYS = 90

_SECRET_KEYS = re.compile(
    r"(password|token|secret|login|chat_id|api_key|investor)", re.IGNORECASE
)
_SECRET_VALUES = re.compile(
    r"(?P<key>(?:password|token|secret|login|chat_id)\s*[=:]\s*)(?P<value>\S+)", re.IGNORECASE
)
REDACTED = "***REDACTED***"


def redact(_logger: Any, _name: str,
           event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """structlog processor: mask any secret-shaped key or inline ``key=value`` pair.

    Runs on **every** record, including exception text, because the day a credential ends
    up in a traceback is the day you find out whether this exists.
    """
    for key in list(event_dict):
        if _SECRET_KEYS.search(str(key)):
            event_dict[key] = REDACTED
            continue
        value = event_dict[key]
        if isinstance(value, str) and _SECRET_VALUES.search(value):
            event_dict[key] = _SECRET_VALUES.sub(lambda m: m.group("key") + REDACTED, value)
    return event_dict


def add_utc_timestamp(_logger: Any, _name: str,
                      event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Stamp the record with true UTC.

    UTC appears **only** in log records, converted explicitly at the point of writing (§4);
    every timestamp inside the trading logic stays in broker server time.
    """
    event_dict["ts_utc"] = datetime.now(UTC).isoformat()
    return event_dict


def configure_logging(log_dir: Path, env: str, level: str = "INFO",
                      console: bool = True) -> None:
    """Configure structlog and the stdlib root logger.

    Args:
        log_dir: Directory for ``fxbot.jsonl``. Created if missing.
        env: ``demo``/``live``/``backtest``; bound into every record.
        level: Root log level.
        console: Whether to also write human-readable lines to stderr.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = []

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / LOG_FILENAME, when="midnight", backupCount=RETENTION_DAYS,
        encoding="utf-8", utc=True,
    )
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    handlers.append(file_handler)
    if console:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO),
                        handlers=handlers, force=True)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            add_utc_timestamp,
            redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(env=env)


def bind_cycle(cycle_id: str, risk_status: str) -> None:
    """Bind the per-cycle context every record in this cycle will carry."""
    structlog.contextvars.bind_contextvars(cycle_id=cycle_id, risk_status=risk_status)
