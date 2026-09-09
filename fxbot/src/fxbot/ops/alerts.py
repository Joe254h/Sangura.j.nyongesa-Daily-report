"""Severity-gated alerting to Telegram or a webhook (§14.2).

**Alerting must never raise into the trading loop.** Every send is wrapped and failures
are logged, not propagated: an outage at Telegram is not a reason to stop trading, and it
is certainly not a reason to crash mid-order.
"""

from __future__ import annotations

from enum import IntEnum

import httpx
import structlog

log = structlog.get_logger(__name__)

TELEGRAM_TIMEOUT_S = 10.0
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class Severity(IntEnum):
    """Alert severities, ordered so gating is a comparison."""

    INFO = 10
    WARNING = 20
    CRITICAL = 30


class Alerter:
    """Sends alerts at or above a configured severity."""

    def __init__(self, enabled: bool, min_severity: str, bot_token: str | None,
                 chat_id: str | None, env: str) -> None:
        """Build the alerter.

        Args:
            enabled: Master switch from config.
            min_severity: ``INFO``/``WARNING``/``CRITICAL``.
            bot_token: ``TELEGRAM_BOT_TOKEN`` from the environment, never from YAML.
            chat_id: ``TELEGRAM_CHAT_ID``.
            env: Environment name, prefixed onto every message so a demo alert is never
                mistaken for a live one.
        """
        self._enabled = enabled
        self._min = Severity[min_severity.upper()]
        self._token = bot_token
        self._chat = chat_id
        self._env = env
        self.sent: list[tuple[Severity, str]] = []

    def _send(self, severity: Severity, message: str) -> None:
        """Deliver one alert, swallowing transport failures."""
        self.sent.append((severity, message))
        if not self._enabled or severity < self._min:
            return
        text = f"[{self._env.upper()}][{severity.name}] {message}"
        log.info("alert", severity=severity.name, message=message)
        if not (self._token and self._chat):
            return
        try:
            httpx.post(TELEGRAM_API.format(token=self._token),
                       json={"chat_id": self._chat, "text": text},
                       timeout=TELEGRAM_TIMEOUT_S)
        except httpx.HTTPError as exc:
            log.warning("alert_delivery_failed", error=str(exc), severity=severity.name)

    def info(self, message: str) -> None:
        """Send an INFO alert: each fill and each close."""
        self._send(Severity.INFO, message)

    def warning(self, message: str) -> None:
        """Send a WARNING: lockout, reduced size, rejected order, quality failure."""
        self._send(Severity.WARNING, message)

    def critical(self, message: str) -> None:
        """Send a CRITICAL: any HALTED transition, NO_MONEY, reconciliation failure."""
        self._send(Severity.CRITICAL, message)

    def digest(self, lines: list[str]) -> None:
        """Send the daily digest at broker 00:05 (§14.2)."""
        self._send(Severity.INFO, "Daily digest\n" + "\n".join(lines))
