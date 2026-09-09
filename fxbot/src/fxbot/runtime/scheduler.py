"""Bar-close scheduling on broker server time (§10.1).

The bot wakes shortly after each H1 bar closes so the bar is final on the server. Between
bars it sleeps; it does not poll ticks. A single-threaded loop, because ``MetaTrader5`` is
not thread-safe and not process-shareable -- exactly one process owns the connection
(§1.1, §17.19).

The wall-clock read the scheduler needs is **injected** (§0.6): ``ops/`` supplies
:func:`fxbot.ops.health.utc_now`, and the clock converts it to server time. Nothing here
calls ``datetime.now()``.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import structlog

from fxbot.config.schema import AppConfig
from fxbot.core.clock import ServerClock
from fxbot.core.errors import FatalError
from fxbot.ops.alerts import Alerter
from fxbot.risk.governor import RiskGovernor
from fxbot.runtime.engine import TradingEngine

log = structlog.get_logger(__name__)


class Scheduler:
    """Wakes the engine just after each bar close and keeps the loop alive."""

    def __init__(
        self,
        cfg: AppConfig,
        clock: ServerClock,
        engine: TradingEngine,
        governor: RiskGovernor,
        alerter: Alerter,
        utc_now: Callable[[], datetime],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Wire the scheduler.

        Args:
            cfg: The resolved configuration.
            clock: The broker clock.
            engine: The engine to drive.
            governor: The governor, halted on a fatal error.
            alerter: For CRITICAL notifications.
            utc_now: The injected true-UTC reader (§0.6).
            sleep: Injected so tests do not actually wait.
        """
        self.cfg = cfg
        self.clock = clock
        self.engine = engine
        self.governor = governor
        self.alerter = alerter
        self._utc_now = utc_now
        self._sleep = sleep
        self._shutdown = False

    @property
    def shutdown_requested(self) -> bool:
        """Whether a graceful shutdown has been asked for.

        A property, not a plain attribute: the flag is flipped from a signal handler at an
        arbitrary point, so a reader must never assume the value it saw a moment ago still
        holds.
        """
        return self._shutdown

    def install_signal_handlers(self) -> None:
        """Handle SIGTERM/SIGINT so a service stop finishes the current cycle (§13.4)."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                log.warning("signal_handler_unavailable", signal=sig)

    def _on_signal(self, signum: int, _frame: object) -> None:
        """Request a graceful shutdown. Never kill mid-order."""
        log.info("shutdown_requested", signal=signum)
        self._shutdown = True

    def server_now(self) -> datetime:
        """Return broker server time, derived from the injected UTC read."""
        now = self.clock.from_utc(self._utc_now())
        self.clock.observe(now)
        return now

    def next_wake(self, now: datetime) -> datetime:
        """Return the next wake instant: the next bar close plus the post-close delay."""
        close = self.clock.next_bar_close(now, self.cfg.timeframe_minutes)
        return close + timedelta(seconds=self.cfg.runtime.post_close_delay_s)

    def run_forever(self, max_cycles: int | None = None) -> int:
        """Drive the engine until shutdown.

        Args:
            max_cycles: Stop after this many cycles. Used by tests and by the dry-run
                soak; None means run until signalled.

        Returns:
            The number of cycles **attempted**. A cycle that halted the bot still ran and
            still counts: reporting zero for a run that reached the broker and then tripped
            the kill switch would be a lie the operator has to unpick from the logs.
        """
        completed = 0
        while not self.shutdown_requested:
            if max_cycles is not None and completed >= max_cycles:
                break
            target = self.next_wake(self.server_now())
            if self._sleep_until(target):
                break
            completed += 1
            try:
                self.engine.run_cycle()
            except FatalError as exc:
                self.governor.halt(str(exc))
                self.alerter.critical(f"HALTED: {exc}")
                log.critical("fatal_error", error=str(exc), exc_info=True)
                break
            except Exception as exc:  # noqa: BLE001 - the loop must survive one bad bar
                # Deliberately broad and deliberately *not* silent (§17.18): the next bar
                # gets a fresh attempt, and the operator gets told this one failed.
                log.exception("cycle_failed")
                self.alerter.warning(f"cycle failed: {exc}")
        self.governor.save()
        return completed

    def _sleep_until(self, target: datetime) -> bool:
        """Sleep in short slices so a shutdown signal is noticed promptly.

        One second at a time rather than one long sleep: a service stop has to be noticed
        within a second, not at the next bar close.

        Args:
            target: The server time to wake at.

        Returns:
            True if a shutdown was requested while sleeping, so the caller can leave the
            loop without re-reading a flag a signal handler may flip at any moment.
        """
        while True:
            if self.shutdown_requested:
                return True
            remaining = (target - self.server_now()).total_seconds()
            if remaining <= 0.0:
                return False
            self._sleep(min(remaining, 1.0))
