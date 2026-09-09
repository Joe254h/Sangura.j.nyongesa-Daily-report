"""Persisted risk state (§8.5).

The kill switch survives restarts (§0.5). A crash-restart must not clear a daily lockout,
and a torn write must not lose one either -- every save is write-to-``.tmp`` then
:meth:`pathlib.Path.replace`, which is atomic on both NTFS and POSIX.

A corrupt state file is **not** recoverable by starting clean: that is precisely the
failure mode that turns "the bot is locked out" into "the bot is trading again". The
loader raises, and the governor turns that into ``HALTED``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fxbot.core.enums import RiskStatus

SCHEMA_VERSION = 1


class RiskStateCorruptError(Exception):
    """The state file exists but cannot be trusted. Always resolves to ``HALTED``."""


@dataclass
class RiskState:
    """Mutable, persisted risk state: one broker day, plus the counters that outlive it."""

    status: RiskStatus = RiskStatus.NORMAL
    trading_day: date | None = None
    """The broker day this state belongs to."""
    day_start_equity: float = 0.0
    equity_hwm: float = 0.0
    realised_pnl_today: float = 0.0
    """Digest and reporting ONLY. The daily limit is measured on equity including
    floating P/L, never on this field (§8.5)."""
    consecutive_losses: int = 0
    trades_today: int = 0
    halted_reason: str = ""
    halted_at: datetime | None = None
    last_update: datetime | None = None
    schema_version: int = SCHEMA_VERSION
    deposits_today: float = 0.0
    """Balance added by deposit today; the HWM is bumped by it so a top-up does not look
    like a drawdown (§8.5)."""
    last_balance: float = 0.0
    """Previous cycle's balance, used to detect deposits and withdrawals."""
    last_realised_pnl: float = 0.0
    """``realised_pnl_today`` as of the last balance observation. A balance move larger
    than the realised P/L that explains it is cash in or out, not trading."""
    open_tickets: list[int] = field(default_factory=list)
    """Tickets the bot believes are open, for reconciliation across a restart (§8.6)."""

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of this state."""
        raw = asdict(self)
        raw["status"] = str(self.status)
        raw["trading_day"] = self.trading_day.isoformat() if self.trading_day else None
        raw["halted_at"] = self.halted_at.isoformat() if self.halted_at else None
        raw["last_update"] = self.last_update.isoformat() if self.last_update else None
        return raw

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> RiskState:
        """Rebuild a state from its JSON form.

        Args:
            raw: The parsed JSON object.

        Returns:
            The reconstructed state.

        Raises:
            RiskStateCorruptError: On an unknown schema version, an unknown status, or any
                field that will not parse. Every one of these means the kill switch's
                memory is unreliable.
        """
        try:
            version = int(raw["schema_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RiskStateCorruptError("risk state has no readable schema_version") from exc
        if version != SCHEMA_VERSION:
            raise RiskStateCorruptError(
                f"risk state schema {version} != {SCHEMA_VERSION}; migrate deliberately "
                "rather than letting the bot reinterpret an older kill switch"
            )
        try:
            return cls(
                status=RiskStatus(raw["status"]),
                trading_day=(date.fromisoformat(raw["trading_day"])
                             if raw.get("trading_day") else None),
                day_start_equity=float(raw["day_start_equity"]),
                equity_hwm=float(raw["equity_hwm"]),
                realised_pnl_today=float(raw["realised_pnl_today"]),
                consecutive_losses=int(raw["consecutive_losses"]),
                trades_today=int(raw["trades_today"]),
                halted_reason=str(raw.get("halted_reason", "")),
                halted_at=(datetime.fromisoformat(raw["halted_at"])
                           if raw.get("halted_at") else None),
                last_update=(datetime.fromisoformat(raw["last_update"])
                             if raw.get("last_update") else None),
                schema_version=version,
                deposits_today=float(raw.get("deposits_today", 0.0)),
                last_balance=float(raw.get("last_balance", 0.0)),
                last_realised_pnl=float(raw.get("last_realised_pnl", 0.0)),
                open_tickets=[int(t) for t in raw.get("open_tickets", [])],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RiskStateCorruptError(f"risk state is unreadable: {exc}") from exc


def save_state(path: Path, state: RiskState) -> None:
    """Write ``state`` to ``path`` atomically.

    Args:
        path: Destination file, e.g. ``state/risk_state.json``.
        state: The state to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_json(), indent=2, sort_keys=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_state(path: Path) -> RiskState | None:
    """Read the persisted risk state.

    Args:
        path: The state file.

    Returns:
        The state, or None when the file does not exist (a genuinely fresh install).

    Raises:
        RiskStateCorruptError: When the file exists but is unreadable or inconsistent.
    """
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RiskStateCorruptError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RiskStateCorruptError(f"{path} does not contain a JSON object")
    return RiskState.from_json(raw)
