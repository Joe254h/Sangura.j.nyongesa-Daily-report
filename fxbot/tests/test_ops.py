"""Logging, alerting and health tests.

**Why this file exists (§3 note).** §3's test list predates none of this -- ``ops/`` is in
the tree but has no named test file, and §12.1's 80% floor covers it. The redaction filter
in particular is not optional: §13.6 says secrets never appear in logs, and a filter with
no test is a filter you find out about after the fact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from fxbot.ops.alerts import Alerter, Severity
from fxbot.ops.health import Health, utc_now
from fxbot.ops.logging import LOG_FILENAME, REDACTED, bind_cycle, configure_logging, redact

# ---------------------------------------------------------------- logging


def test_redaction_masks_secret_shaped_keys() -> None:
    """Any key that looks like a credential is masked before the record is written."""
    out = redact(None, "info", {"event": "connect", "MT5_PASSWORD": "hunter2",
                                "telegram_bot_token": "abc", "chat_id": 42,
                                "symbol": "EURUSD"})
    assert out["MT5_PASSWORD"] == REDACTED
    assert out["telegram_bot_token"] == REDACTED
    assert out["chat_id"] == REDACTED
    assert out["symbol"] == "EURUSD"


def test_redaction_masks_inline_key_value_pairs() -> None:
    """The day a credential lands inside a traceback string, this is what catches it."""
    out = redact(None, "error", {"event": "boot failed: password=hunter2 login=12345"})
    assert "hunter2" not in out["event"]
    assert "12345" not in out["event"]
    assert REDACTED in out["event"]


def test_configure_logging_writes_json_to_the_log_dir(tmp_path: Path) -> None:
    """JSON to ``logs/fxbot.jsonl``, and the directory is created if missing (§14.1)."""
    import logging

    import structlog

    log_dir = tmp_path / "logs"
    configure_logging(log_dir, "backtest", console=False)
    bind_cycle("cycle-1", "NORMAL")
    structlog.get_logger("test").info("hello", symbol="EURUSD", password="secret")
    for handler in logging.getLogger().handlers:
        handler.flush()

    written = (log_dir / LOG_FILENAME).read_text(encoding="utf-8")
    assert "hello" in written
    assert "EURUSD" in written
    assert "secret" not in written
    assert "cycle-1" in written
    assert "ts_utc" in written
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------- alerts


def test_alerts_are_severity_gated() -> None:
    """``min_severity: WARNING`` means INFO fills do not page anyone (§14.2)."""
    alerter = Alerter(enabled=True, min_severity="WARNING", bot_token=None, chat_id=None,
                      env="demo")
    alerter.info("a fill")
    alerter.warning("a lockout")
    alerter.critical("a halt")
    assert [severity for severity, _ in alerter.sent] == [
        Severity.INFO, Severity.WARNING, Severity.CRITICAL]


def test_alerting_never_raises_into_the_trading_loop(monkeypatch) -> None:  # noqa: ANN001
    """An outage at Telegram is not a reason to stop trading, still less to crash (§14.2)."""
    def explode(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("telegram is down")

    monkeypatch.setattr(httpx, "post", explode)
    alerter = Alerter(enabled=True, min_severity="INFO", bot_token="t", chat_id="c",
                      env="live")
    alerter.critical("HALTED")          # must not raise
    assert alerter.sent[-1][0] is Severity.CRITICAL


def test_a_disabled_alerter_still_records_for_the_digest() -> None:
    """The backtest disables delivery; the messages are still available to assert on."""
    alerter = Alerter(enabled=False, min_severity="CRITICAL", bot_token=None, chat_id=None,
                      env="backtest")
    alerter.warning("quality failure")
    alerter.digest(["equity 10,000", "trades 3"])
    assert len(alerter.sent) == 2
    assert "Daily digest" in alerter.sent[-1][1]


def test_the_environment_is_stamped_on_every_message(monkeypatch) -> None:  # noqa: ANN001
    """A demo alert must never be mistaken for a live one."""
    posted: list[dict] = []

    def capture(url: str, json: dict, timeout: float) -> None:  # noqa: A002
        posted.append(json)

    monkeypatch.setattr(httpx, "post", capture)
    Alerter(True, "INFO", "token", "chat", "demo").warning("something")
    assert posted and posted[0]["text"].startswith("[DEMO][WARNING]")


# ---------------------------------------------------------------- health


def test_utc_now_is_the_only_wall_clock_read() -> None:
    """§0.6: it returns tz-aware UTC, and everything else takes it by injection."""
    now = utc_now()
    assert now.tzinfo is UTC
    assert abs((datetime.now(UTC) - now).total_seconds()) < 5


def test_the_watchdog_fires_after_the_configured_silence() -> None:
    """No completed cycle for ``watchdog_multiples`` bars is an alert (§10.1)."""
    health = Health(None, 60, 3.0)
    start = datetime(2024, 6, 3, 10, tzinfo=UTC)
    assert health.stalled(start) is False, "nothing has run yet; that is not a stall"

    health.heartbeat(start)
    assert health.last_cycle == start
    assert health.stalled(start + timedelta(hours=2)) is False
    assert health.stalled(start + timedelta(hours=4)) is True


def test_a_failed_heartbeat_is_logged_not_raised(monkeypatch) -> None:  # noqa: ANN001
    """A dead-man's switch that is itself down must not take the bot with it."""
    def explode(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectTimeout("healthchecks is down")

    monkeypatch.setattr(httpx, "get", explode)
    health = Health("https://hc.example/ping", 60, 3.0)
    health.heartbeat(datetime(2024, 6, 3, 10, tzinfo=UTC))   # must not raise
    assert health.last_cycle is not None


def test_the_heartbeat_pings_the_configured_url(monkeypatch) -> None:  # noqa: ANN001
    """The absence of this ping is what tells you the VPS died (§14.3)."""
    calls: list[str] = []
    monkeypatch.setattr(httpx, "get",
                        lambda url, timeout: calls.append(url))  # noqa: ARG005
    Health("https://hc.example/ping", 60, 3.0).heartbeat(
        datetime(2024, 6, 3, 10, tzinfo=UTC))
    assert calls == ["https://hc.example/ping"]
