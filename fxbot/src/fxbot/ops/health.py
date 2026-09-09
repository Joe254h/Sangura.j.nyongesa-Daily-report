"""Heartbeat, watchdog and the system's single wall-clock read (§14.3, §0.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import structlog

log = structlog.get_logger(__name__)

HEARTBEAT_TIMEOUT_S = 5.0


def utc_now() -> datetime:
    """Return the current instant in true UTC.

    **This is the only wall-clock read in the system** (§0.6). It is injected into
    :func:`fxbot.data.clock_probe.probe_server_offset` and into the scheduler; nothing in
    ``core/``, ``strategy/``, ``risk/`` or ``data/`` calls a clock of its own, which is
    what makes every day boundary testable under a frozen clock.
    """
    return datetime.now(UTC)


class Health:
    """Dead-man's-switch heartbeat and a stalled-cycle watchdog."""

    def __init__(self, heartbeat_url: str | None, timeframe_minutes: int,
                 watchdog_multiples: float) -> None:
        """Build the health reporter.

        Args:
            heartbeat_url: A healthchecks.io-style URL, or None to disable.
            timeframe_minutes: Bar length, for the watchdog threshold.
            watchdog_multiples: How many bars of silence before the watchdog fires.
        """
        self._url = heartbeat_url
        self._threshold = timedelta(minutes=timeframe_minutes * watchdog_multiples)
        self._last_cycle: datetime | None = None

    def heartbeat(self, when: datetime) -> None:
        """Record a completed cycle and ping the dead-man's switch.

        If the VPS dies, the *absence* of a heartbeat is what tells you: a bot that has
        crashed cannot send an alert about having crashed (§14.3).

        Args:
            when: Server time of the completed cycle.
        """
        self._last_cycle = when
        if not self._url:
            return
        try:
            httpx.get(self._url, timeout=HEARTBEAT_TIMEOUT_S)
        except httpx.HTTPError as exc:
            # A heartbeat failure must never raise into the trading loop.
            log.warning("heartbeat_failed", error=str(exc))

    def stalled(self, now: datetime) -> bool:
        """Return whether no cycle has completed within the watchdog window."""
        if self._last_cycle is None:
            return False
        return (now - self._last_cycle) > self._threshold

    @property
    def last_cycle(self) -> datetime | None:
        """Server time of the last completed cycle."""
        return self._last_cycle
