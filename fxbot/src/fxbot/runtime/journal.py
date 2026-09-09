"""The SQLite trade and decision journal (§10.3).

WAL mode, append-only. **Rejections are recorded too**: the reject-reason histogram is the
primary debugging tool, and "why did it not trade for three weeks?" has to be answerable
in one query. A decision row is never ``UPDATE``d.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from fxbot.core.enums import RiskStatus
from fxbot.core.models import Approval, ClosedTrade, OrderResult, Signal, SizedOrder

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT, regime TEXT NOT NULL,
    bias TEXT NOT NULL, reject_reason TEXT NOT NULL, entry_ref REAL, stop_price REAL,
    atr REAL, adx REAL, diagnostics TEXT NOT NULL, cycle_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_reason ON decisions(reject_reason);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, approval_id TEXT NOT NULL, ok INTEGER NOT NULL, symbol TEXT,
    side TEXT, volume REAL, stop_price REAL, risk_amount REAL, risk_pct REAL,
    reject_reason TEXT NOT NULL, risk_status TEXT NOT NULL, detail TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, approval_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL,
    volume REAL NOT NULL, stop_price REAL, ok INTEGER NOT NULL, retcode INTEGER NOT NULL,
    ticket INTEGER, filled_volume REAL, filled_price REAL, slippage_points REAL,
    comment TEXT, request TEXT, result TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, volume REAL NOT NULL,
    entry_price REAL, exit_price REAL, entry_time TEXT, exit_time TEXT, initial_stop REAL,
    gross_pnl REAL, commission REAL, swap REAL, net_pnl REAL, r_multiple REAL,
    mae_r REAL, mfe_r REAL, exit_reason TEXT, magic INTEGER
);

CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, cycle_id TEXT NOT NULL, duration_s REAL, symbols INTEGER,
    risk_status TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, before_status TEXT NOT NULL, after_status TEXT NOT NULL, detail TEXT
);
"""


class Journal:
    """Implements :class:`~fxbot.core.models.JournalSink` on SQLite."""

    def __init__(self, path: Path) -> None:
        """Open (and create if needed) the journal at ``path``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._cycle_id = ""

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def set_cycle(self, cycle_id: str) -> None:
        """Tag subsequent rows with ``cycle_id``."""
        self._cycle_id = cycle_id

    # ------------------------------------------------------------------ sink

    def record_decision(self, signal: Signal, ctx_meta: Mapping[str, object]) -> None:
        """Append one decision row -- including every rejection."""
        self._conn.execute(
            "INSERT INTO decisions (ts, symbol, side, regime, bias, reject_reason, entry_ref,"
            " stop_price, atr, adx, diagnostics, cycle_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(ctx_meta.get("now", "")), str(ctx_meta.get("symbol", "")),
                None if signal.side is None else str(signal.side), str(signal.regime),
                str(signal.bias), str(signal.reason), signal.entry_ref, signal.stop_price,
                _finite(signal.atr), _finite(signal.adx),
                json.dumps({k: _finite(v) for k, v in signal.diagnostics.items()}),
                self._cycle_id,
            ),
        )
        self._conn.commit()

    def record_approval(self, approval: Approval) -> None:
        """Append one approval row, whether or not it approved anything."""
        order = approval.order
        self._conn.execute(
            "INSERT INTO approvals (ts, approval_id, ok, symbol, side, volume, stop_price,"
            " risk_amount, risk_pct, reject_reason, risk_status, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                approval.created_at.isoformat(), approval.approval_id, int(approval.ok),
                order.symbol if order else None,
                str(order.side) if order else None,
                order.volume if order else None,
                order.stop_price if order else None,
                order.risk_amount if order else None,
                order.risk_pct if order else None,
                str(approval.reason), str(approval.risk_status), approval.detail,
            ),
        )
        self._conn.commit()

    def record_order(self, order: SizedOrder, result: OrderResult,
                     request: Mapping[str, Any] | None = None,
                     raw_result: Any = None) -> None:
        """Append one order row with the full request and result.

        When something goes wrong at 3am you get exactly one chance to reconstruct it from
        the logs, so the whole request dict is stored, not a summary (§9.4).
        """
        self._conn.execute(
            "INSERT INTO orders (ts, approval_id, symbol, side, volume, stop_price, ok,"
            " retcode, ticket, filled_volume, filled_price, slippage_points, comment,"
            " request, result) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now().isoformat(), order.approval_id, order.symbol, str(order.side),
                order.volume, order.stop_price, int(result.ok), result.retcode, result.ticket,
                result.filled_volume, result.filled_price, result.slippage_points,
                result.comment, json.dumps(_safe(request)), json.dumps(_safe(raw_result)),
            ),
        )
        self._conn.commit()

    def record_trade(self, trade: ClosedTrade) -> None:
        """Append one completed round trip."""
        self._conn.execute(
            "INSERT INTO trades (ticket, symbol, side, volume, entry_price, exit_price,"
            " entry_time, exit_time, initial_stop, gross_pnl, commission, swap, net_pnl,"
            " r_multiple, mae_r, mfe_r, exit_reason, magic)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade.ticket, trade.symbol, str(trade.side), trade.volume, trade.entry_price,
                trade.exit_price, trade.entry_time.isoformat(), trade.exit_time.isoformat(),
                trade.initial_stop, trade.gross_pnl, trade.commission, trade.swap,
                trade.net_pnl, trade.r_multiple, trade.mae_r, trade.mfe_r, trade.exit_reason,
                trade.magic,
            ),
        )
        self._conn.commit()

    def record_risk_event(self, before: RiskStatus, after: RiskStatus, detail: str) -> None:
        """Append one risk-status transition with the numbers that caused it."""
        self._conn.execute(
            "INSERT INTO risk_events (ts, before_status, after_status, detail) VALUES (?,?,?,?)",
            (datetime.now().isoformat(), str(before), str(after), detail),
        )
        self._conn.commit()

    def record_cycle(self, cycle_id: str, duration_s: float, symbols: int,
                     risk_status: RiskStatus, error: str = "") -> None:
        """Append one heartbeat row for a completed cycle."""
        self._conn.execute(
            "INSERT INTO cycles (ts, cycle_id, duration_s, symbols, risk_status, error)"
            " VALUES (?,?,?,?,?,?)",
            (datetime.now().isoformat(), cycle_id, duration_s, symbols, str(risk_status), error),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ queries

    def reject_histogram(self, since: str | None = None) -> dict[str, int]:
        """Return ``reject_reason -> count``, the primary debugging view (§10.3)."""
        sql = "SELECT reject_reason, COUNT(*) FROM decisions"
        args: tuple[Any, ...] = ()
        if since:
            sql += " WHERE ts >= ?"
            args = (since,)
        sql += " GROUP BY reject_reason ORDER BY COUNT(*) DESC"
        return dict(self._conn.execute(sql, args).fetchall())

    def open_position_stops(self) -> dict[int, float]:
        """Return ``ticket -> initial_stop`` for reconstructing adopted positions (§8.6)."""
        rows = self._conn.execute(
            "SELECT ticket, stop_price FROM orders WHERE ok = 1 AND ticket IS NOT NULL"
        ).fetchall()
        return {int(t): float(s) for t, s in rows if s is not None}

    def entry_regimes(self) -> dict[tuple[str, datetime], str]:
        """Return ``(symbol, decision time) -> regime`` for decisions that took a trade.

        A decision's timestamp is the close of its signal bar, which is the same instant
        as the fill bar's open -- so this joins straight onto ``ClosedTrade.entry_time``
        and gives the per-regime breakdown §11.3 asks for without widening the trade
        contract.
        """
        rows = self._conn.execute(
            "SELECT symbol, ts, regime FROM decisions WHERE side IS NOT NULL"
        ).fetchall()
        out: dict[tuple[str, datetime], str] = {}
        for symbol, ts, regime in rows:
            try:
                when = datetime.fromisoformat(str(ts))
            except ValueError:
                continue
            out[(str(symbol), when)] = str(regime)
        return out

    def recorded_tickets(self) -> set[int]:
        """Return every ticket that already has a closed-trade row."""
        rows = self._conn.execute("SELECT DISTINCT ticket FROM trades").fetchall()
        return {int(r[0]) for r in rows}


class NullJournal:
    """A :class:`~fxbot.core.models.JournalSink` that discards everything.

    Used by the walk-forward optimiser, which runs thousands of parameter sets and has no
    use for a 40 GB SQLite file. It is a *sink*, not a silencer: the backtest report is
    still built from the returned trade list.
    """

    def record_decision(self, signal: Signal, ctx_meta: Mapping[str, object]) -> None:
        """Discard the decision."""

    def record_approval(self, approval: Approval) -> None:
        """Discard the approval."""

    def record_order(self, order: SizedOrder, result: OrderResult) -> None:
        """Discard the order."""

    def record_trade(self, trade: ClosedTrade) -> None:
        """Discard the trade."""

    def record_risk_event(self, before: RiskStatus, after: RiskStatus, detail: str) -> None:
        """Discard the risk event."""


def _finite(value: Any) -> float | None:
    """Return ``value`` as a float, or None when it is NaN/inf -- SQLite hates both."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _safe(value: Any) -> Any:
    """Return a JSON-serialisable view of ``value``."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
