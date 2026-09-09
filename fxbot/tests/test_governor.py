"""Risk-governor tests (§12.3).

The deadlock regression is first, because it is the failure that bricks the bot
permanently and the persisted state makes a restart no help at all.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from tests.conftest import (
    SERVER_TZ,
    load_bars,
    make_account,
    make_context,
    make_position,
)

from fxbot.core.clock import ServerClock
from fxbot.core.enums import RejectReason, RiskStatus, Side
from fxbot.core.models import ClosedTrade, SizedOrder
from fxbot.risk.governor import RiskGovernor
from fxbot.risk.state import RiskState, RiskStateCorruptError, load_state, save_state

DAY_ONE = datetime(2024, 6, 3, 9, 0, tzinfo=SERVER_TZ)
DAY_TWO = datetime(2024, 6, 4, 9, 0, tzinfo=SERVER_TZ)


def build(cfg, journal, tmp_path: Path, clock: ServerClock | None = None) -> RiskGovernor:
    """Build and load a governor against a throwaway state file."""
    c = clock or ServerClock(3)
    c.observe(DAY_ONE)
    governor = RiskGovernor(cfg, tmp_path / "risk_state.json", c, journal)
    governor.load()
    governor.refresh(make_account(10_000.0, DAY_ONE), [])
    return governor


def losing_trade(ticket: int, pnl: float = -50.0, when: datetime = DAY_ONE) -> ClosedTrade:
    """A completed losing round trip."""
    return ClosedTrade(
        ticket=ticket, symbol="EURUSD", side=Side.BUY, volume=0.1, entry_price=1.08,
        exit_price=1.078, entry_time=when, exit_time=when, initial_stop=1.078,
        gross_pnl=pnl, commission=0.7, swap=0.0, net_pnl=pnl, r_multiple=-1.0,
        mae_r=-1.0, mfe_r=0.2, exit_reason="stop", magic=990117,
    )


# ---------------------------------------------------------------- the deadlock regression


def test_new_day_clears_the_loss_streak_and_refresh_does_not_relock(cfg, journal,
                                                                    tmp_path) -> None:
    """Five losses lock out; the next broker day clears it and it stays cleared.

    Written first, per §12.3. Without the ``consecutive_losses`` reset in
    :meth:`on_new_day`, ``refresh()`` re-evaluates ``>= 5`` on the very next cycle and
    re-locks. The counter can only fall on a winning trade, a winning trade needs an entry,
    and entries are blocked in ``DAILY_LOCKOUT``: the bot bricks itself permanently, and
    because §0.5 persists the state a restart does not clear it.
    """
    governor = build(cfg, journal, tmp_path)
    for ticket in range(1, 6):
        governor.record_closed_trade(losing_trade(ticket))
    assert governor.status is RiskStatus.DAILY_LOCKOUT

    governor.on_new_day(make_account(9_750.0, DAY_TWO))
    assert governor.status is RiskStatus.NORMAL
    assert governor.state.consecutive_losses == 0

    # The very next cycle must not re-lock.
    assert governor.refresh(make_account(9_750.0, DAY_TWO), []) is RiskStatus.NORMAL
    assert governor.refresh(make_account(9_750.0, DAY_TWO), []) is RiskStatus.NORMAL


def test_reduced_also_clears_on_the_new_day(cfg, journal, tmp_path) -> None:
    """The same deadlock exists one threshold lower, and is closed the same way."""
    governor = build(cfg, journal, tmp_path)
    for ticket in range(1, 4):
        governor.record_closed_trade(losing_trade(ticket))
    assert governor.status is RiskStatus.REDUCED
    assert governor.size_multiplier() == pytest.approx(0.5)

    governor.on_new_day(make_account(9_850.0, DAY_TWO))
    assert governor.status is RiskStatus.NORMAL
    assert governor.refresh(make_account(9_850.0, DAY_TWO), []) is RiskStatus.NORMAL
    assert governor.size_multiplier() == pytest.approx(1.0)


# ---------------------------------------------------------------- limits


def test_daily_limit_trips_at_exactly_three_percent(cfg, journal, tmp_path) -> None:
    """Measured on equity including floating P/L, not on realised P/L (§8.5)."""
    governor = build(cfg, journal, tmp_path)
    assert governor.refresh(make_account(9_701.0, DAY_ONE), []) is RiskStatus.NORMAL
    assert governor.refresh(make_account(9_700.0, DAY_ONE), []) is RiskStatus.DAILY_LOCKOUT
    assert governor.state.realised_pnl_today == 0.0


def test_daily_lockout_does_not_close_positions_by_default(cfg) -> None:
    """Closing on the limit realises floating losses at the worst moment (§8.5)."""
    assert cfg.risk.flatten_on_daily_lockout is False


def test_max_drawdown_from_the_hwm_halts(cfg, journal, tmp_path) -> None:
    """A 10% fall from the equity high-water mark halts, and only a manual reset leaves."""
    governor = build(cfg, journal, tmp_path)
    governor.refresh(make_account(12_000.0, DAY_ONE), [])
    assert governor.state.equity_hwm == pytest.approx(12_000.0)
    assert governor.refresh(make_account(10_800.0, DAY_ONE), []) is RiskStatus.HALTED

    governor.on_new_day(make_account(10_800.0, DAY_TWO))
    assert governor.status is RiskStatus.HALTED, "a new day never clears HALTED"


def test_a_deposit_bumps_the_hwm_instead_of_tripping_max_drawdown(cfg, journal,
                                                                  tmp_path) -> None:
    """A top-up must not read as a drawdown; a withdrawal must not lower the HWM (§8.5)."""
    governor = build(cfg, journal, tmp_path)
    governor.refresh(make_account(10_000.0, DAY_ONE, balance=10_000.0), [])
    before = governor.state.equity_hwm

    governor.refresh(make_account(15_000.0, DAY_ONE, balance=15_000.0), [])
    assert governor.state.equity_hwm == pytest.approx(15_000.0)
    assert governor.state.deposits_today == pytest.approx(5_000.0)
    assert governor.status is RiskStatus.NORMAL
    assert governor.state.equity_hwm > before

    hwm = governor.state.equity_hwm
    governor.refresh(make_account(14_000.0, DAY_ONE, balance=14_000.0), [])
    assert governor.state.equity_hwm == pytest.approx(hwm), "a withdrawal must not lower it"


def test_five_consecutive_losses_lock_out_and_three_reduce(cfg, journal, tmp_path) -> None:
    """The consecutive-loss rule trips before the daily limit, by design (§8.1)."""
    governor = build(cfg, journal, tmp_path)
    for ticket in range(1, 3):
        governor.record_closed_trade(losing_trade(ticket))
    assert governor.status is RiskStatus.NORMAL
    governor.record_closed_trade(losing_trade(3))
    assert governor.status is RiskStatus.REDUCED
    for ticket in (4, 5):
        governor.record_closed_trade(losing_trade(4 + ticket))
    assert governor.status is RiskStatus.DAILY_LOCKOUT


def test_a_win_clears_the_streak_and_restores_normal(cfg, journal, tmp_path) -> None:
    """A winning trade resets the counter; a scratch trade leaves it alone."""
    governor = build(cfg, journal, tmp_path)
    for ticket in range(1, 4):
        governor.record_closed_trade(losing_trade(ticket))
    assert governor.status is RiskStatus.REDUCED
    governor.record_closed_trade(losing_trade(9, pnl=0.0))
    assert governor.state.consecutive_losses == 3, "a scratch is neither a win nor a loss"
    governor.record_closed_trade(losing_trade(10, pnl=120.0))
    assert governor.state.consecutive_losses == 0
    assert governor.status is RiskStatus.NORMAL


# ---------------------------------------------------------------- persistence


def test_the_lockout_survives_a_restart(cfg, journal, tmp_path) -> None:
    """Write state, construct a new governor, assert still locked (§0.5, §12.3)."""
    governor = build(cfg, journal, tmp_path)
    for ticket in range(1, 6):
        governor.record_closed_trade(losing_trade(ticket))
    assert governor.status is RiskStatus.DAILY_LOCKOUT

    clock = ServerClock(3)
    clock.observe(DAY_ONE)
    reborn = RiskGovernor(cfg, tmp_path / "risk_state.json", clock, journal)
    reborn.load()
    assert reborn.status is RiskStatus.DAILY_LOCKOUT
    assert reborn.state.consecutive_losses == 5
    assert reborn.blocks_entries()


def test_a_corrupt_state_file_halts_rather_than_starting_clean(cfg, journal,
                                                               tmp_path) -> None:
    """Never start clean after a corrupt state file: that is how a kill switch un-trips."""
    path = tmp_path / "risk_state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    clock = ServerClock(3)
    clock.observe(DAY_ONE)
    governor = RiskGovernor(cfg, path, clock, journal)
    governor.load()
    assert governor.status is RiskStatus.HALTED
    assert "corrupt" in governor.state.halted_reason


def test_an_unknown_schema_version_is_corrupt(tmp_path: Path) -> None:
    """A future state file is not silently reinterpreted."""
    path = tmp_path / "risk_state.json"
    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(RiskStateCorruptError):
        load_state(path)


def test_state_writes_are_atomic(tmp_path: Path) -> None:
    """A save leaves no temporary file behind and round-trips exactly."""
    path = tmp_path / "risk_state.json"
    state = RiskState(status=RiskStatus.DAILY_LOCKOUT, trading_day=date(2024, 6, 3),
                      day_start_equity=10_000.0, equity_hwm=11_000.0, consecutive_losses=5)
    save_state(path, state)
    assert list(tmp_path.glob("*.tmp")) == []
    restored = load_state(path)
    assert restored is not None
    assert restored.status is RiskStatus.DAILY_LOCKOUT
    assert restored.consecutive_losses == 5
    assert restored.trading_day == date(2024, 6, 3)


def test_missing_state_file_is_a_fresh_normal_start(tmp_path: Path) -> None:
    """A genuinely fresh install is not an error."""
    assert load_state(tmp_path / "nothing.json") is None


# ---------------------------------------------------------------- approvals


def test_approve_is_the_only_way_to_get_a_sized_order(cfg, journal, tmp_path,
                                                      eurusd, clock) -> None:
    """A clean signal produces an order carrying an approval id (§0.4)."""
    from fxbot.strategy.trend_donchian import generate_signal

    governor = build(cfg, journal, tmp_path, clock)
    governor.set_symbol_specs({"EURUSD": eurusd})
    ctx = make_context(load_bars("clean_long"), eurusd, cfg, clock)
    signal = generate_signal(ctx)
    assert signal.side is Side.BUY

    approval = governor.approve(signal, ctx, make_account(10_000.0, ctx.now), [])
    assert approval.ok
    assert approval.order is not None
    assert approval.order.approval_id == approval.approval_id
    assert approval.order.volume > 0.0
    assert approval.order.take_profit is None, "no server-side TP in v1 (§7.4)"


def test_approve_refuses_in_lockout_and_when_halted(cfg, journal, tmp_path, eurusd,
                                                    clock) -> None:
    """Every blocked status refuses with its own reason, and never returns an order."""
    from fxbot.strategy.trend_donchian import generate_signal

    governor = build(cfg, journal, tmp_path, clock)
    ctx = make_context(load_bars("clean_long"), eurusd, cfg, clock)
    signal = generate_signal(ctx)
    account = make_account(10_000.0, ctx.now)

    governor.state.status = RiskStatus.DAILY_LOCKOUT
    blocked = governor.approve(signal, ctx, account, [])
    assert not blocked.ok and blocked.reason is RejectReason.DAILY_LOSS_LIMIT
    assert blocked.order is None

    governor.halt("test")
    halted = governor.approve(signal, ctx, account, [])
    assert not halted.ok and halted.reason is RejectReason.KILL_SWITCH


def test_margin_gate_uses_the_brokers_number(cfg, journal, tmp_path, eurusd, clock) -> None:
    """Never compute margin from leverage; reject when the broker's number is too big."""
    from fxbot.strategy.trend_donchian import generate_signal

    governor = build(cfg, journal, tmp_path, clock)
    ctx = make_context(load_bars("clean_long"), eurusd, cfg, clock)
    signal = generate_signal(ctx)
    account = make_account(10_000.0, ctx.now)

    governor.set_margin_calculator(lambda order: 5_000.0)  # noqa: ARG005
    refused = governor.approve(signal, ctx, account, [])
    assert not refused.ok and refused.reason is RejectReason.MARGIN_INSUFFICIENT

    governor.set_margin_calculator(lambda order: 100.0)  # noqa: ARG005
    assert governor.approve(signal, ctx, account, []).ok


def test_manual_reset_requires_an_operator(cfg, journal, tmp_path) -> None:
    """An unattributed reset of a kill switch is a hole in the audit trail."""
    governor = build(cfg, journal, tmp_path)
    governor.halt("blown up")
    with pytest.raises(ValueError):
        governor.manual_reset("   ")
    governor.manual_reset("sangura")
    assert governor.status is RiskStatus.NORMAL
    assert governor.state.halted_reason == ""


def test_refresh_before_load_is_a_programming_error(cfg, journal, tmp_path) -> None:
    """Refreshing before the kill switch has been read is refused, loudly."""
    clock = ServerClock(3)
    clock.observe(DAY_ONE)
    governor = RiskGovernor(cfg, tmp_path / "risk_state.json", clock, journal)
    with pytest.raises(RuntimeError, match="kill switch is unread"):
        governor.refresh(make_account(10_000.0, DAY_ONE), [])


def test_record_fill_only_counts_a_real_fill(cfg, journal, tmp_path) -> None:
    """A rejected send is not a trade and does not enter the open-ticket set."""
    from fxbot.core.models import OrderResult

    governor = build(cfg, journal, tmp_path)
    order = SizedOrder(symbol="EURUSD", side=Side.BUY, volume=0.1, stop_price=1.07,
                       take_profit=None, risk_amount=50.0, risk_pct=0.5, approval_id="abc")
    governor.record_fill(order, OrderResult(False, 10006, None, 0.0, 0.0, 0.0, "reject", "x"))
    assert governor.state.trades_today == 0
    governor.record_fill(order, OrderResult(True, 10009, 77, 0.1, 1.08, 0.0, "done", "x"))
    assert governor.state.trades_today == 1
    assert governor.state.open_tickets == [77]


def test_a_state_file_missing_its_version_is_corrupt(tmp_path: Path) -> None:
    """No schema version means the kill switch's memory cannot be trusted."""
    path = tmp_path / "risk_state.json"
    path.write_text(json.dumps({"status": "NORMAL"}), encoding="utf-8")
    with pytest.raises(RiskStateCorruptError, match="schema_version"):
        load_state(path)


def test_a_state_file_with_bad_fields_is_corrupt(tmp_path: Path) -> None:
    """A readable version but unreadable fields is still a corrupt kill switch."""
    path = tmp_path / "risk_state.json"
    path.write_text(json.dumps({"schema_version": 1, "status": "NOT_A_STATUS"}),
                    encoding="utf-8")
    with pytest.raises(RiskStateCorruptError, match="unreadable"):
        load_state(path)


def test_a_state_file_that_is_not_an_object_is_corrupt(tmp_path: Path) -> None:
    """Valid JSON is not the same as a valid state."""
    path = tmp_path / "risk_state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(RiskStateCorruptError, match="JSON object"):
        load_state(path)


def test_a_failed_save_leaves_no_temporary_file(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """A torn write must not litter the state directory with half-written files."""

    path = tmp_path / "risk_state.json"

    def explode(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", lambda self, target: explode(self, target))
    with pytest.raises(OSError, match="disk full"):
        save_state(path, RiskState())
    assert list(tmp_path.glob("*.tmp")) == []
    assert not path.exists()


def test_approve_refuses_a_signal_with_no_side(cfg, journal, tmp_path, eurusd, clock) -> None:
    """Defence in depth: a rejection can never be turned into an order."""
    from fxbot.core.enums import Bias, Regime
    from fxbot.core.models import Signal

    governor = build(cfg, journal, tmp_path, clock)
    ctx = make_context(load_bars("clean_long"), eurusd, cfg, clock)
    empty = Signal(side=None, regime=Regime.RANGING, bias=Bias.NEUTRAL, entry_ref=1.08,
                   stop_price=0.0, atr=float("nan"), adx=float("nan"),
                   reason=RejectReason.NO_TRIGGER, diagnostics={})
    approval = governor.approve(empty, ctx, make_account(10_000.0, ctx.now), [])
    assert not approval.ok
    assert approval.order is None


def test_approve_refuses_when_the_account_is_too_small(cfg, journal, tmp_path, eurusd,
                                                       clock) -> None:
    """Below the minimum lot the governor refuses rather than rounding up (§17.7)."""
    from fxbot.strategy.trend_donchian import generate_signal

    governor = build(cfg, journal, tmp_path, clock)
    ctx = make_context(load_bars("clean_long"), eurusd, cfg, clock)
    signal = generate_signal(ctx)
    approval = governor.approve(signal, ctx, make_account(50.0, ctx.now), [])
    assert not approval.ok
    assert approval.reason is RejectReason.SIZE_BELOW_MIN


def test_approve_refuses_on_an_exposure_rule(cfg, journal, tmp_path, eurusd, clock,
                                             specs) -> None:
    """The exposure verdict comes back as the approval's reason, verbatim (§8.4)."""
    from fxbot.strategy.trend_donchian import generate_signal

    governor = build(cfg, journal, tmp_path, clock)
    governor.set_symbol_specs(specs)
    ctx = make_context(load_bars("clean_long"), eurusd, cfg, clock)
    signal = generate_signal(ctx)
    open_positions = [
        make_position(specs["EURUSD"], Side.BUY, 1.08, 1.076,
                      datetime(2024, 3, 1, tzinfo=SERVER_TZ), ticket=1),
    ]
    approval = governor.approve(signal, ctx, make_account(10_000.0, ctx.now),
                                open_positions)
    assert not approval.ok
    assert approval.reason is RejectReason.SYMBOL_ALREADY_OPEN


def test_the_hwm_only_ever_rises(cfg, journal, tmp_path) -> None:
    """Monotonic and persisted forever, across days and restarts (§8.5)."""
    governor = build(cfg, journal, tmp_path)
    governor.refresh(make_account(11_000.0, DAY_ONE), [])
    assert governor.state.equity_hwm == pytest.approx(11_000.0)
    governor.refresh(make_account(10_500.0, DAY_ONE), [])
    assert governor.state.equity_hwm == pytest.approx(11_000.0)
    governor.on_new_day(make_account(10_500.0, DAY_TWO))
    assert governor.state.equity_hwm == pytest.approx(11_000.0)


def test_a_governor_with_no_observed_clock_still_loads(cfg, journal, tmp_path) -> None:
    """Boot order: load() runs before the first account snapshot (§8.5)."""
    governor = RiskGovernor(cfg, tmp_path / "risk_state.json", ServerClock(3), journal)
    governor.load()
    assert governor.status is RiskStatus.NORMAL
    governor.halt("halted before the first tick")
    assert governor.state.halted_at is None
    assert governor.status is RiskStatus.HALTED


def test_a_new_day_that_opens_at_a_new_high_raises_the_hwm(cfg, journal, tmp_path) -> None:
    """The HWM tracks equity across the rollover, not just within a day."""
    governor = build(cfg, journal, tmp_path)
    assert governor.state.equity_hwm == pytest.approx(10_000.0)
    governor.on_new_day(make_account(12_500.0, DAY_TWO))
    assert governor.state.equity_hwm == pytest.approx(12_500.0)
    assert governor.state.day_start_equity == pytest.approx(12_500.0)
    assert governor.state.trades_today == 0


def test_refresh_re_derives_the_status_from_the_loss_counter(cfg, journal,
                                                             tmp_path) -> None:
    """The counter is authoritative on every cycle, not only when a trade closes.

    A restart reloads ``consecutive_losses`` from disk without replaying the trades, so
    :meth:`refresh` has to reach the same verdict :meth:`record_closed_trade` did.
    """
    governor = build(cfg, journal, tmp_path)

    governor.state.consecutive_losses = 3
    assert governor.refresh(make_account(9_900.0, DAY_ONE), []) is RiskStatus.REDUCED

    governor.state.consecutive_losses = 5
    assert governor.refresh(make_account(9_900.0, DAY_ONE), []) is RiskStatus.DAILY_LOCKOUT


def test_refresh_clears_reduced_once_the_streak_is_broken(cfg, journal, tmp_path) -> None:
    """REDUCED is not sticky: it follows the counter down as well as up."""
    governor = build(cfg, journal, tmp_path)
    governor.state.consecutive_losses = 3
    assert governor.refresh(make_account(9_900.0, DAY_ONE), []) is RiskStatus.REDUCED
    governor.state.consecutive_losses = 0
    assert governor.refresh(make_account(9_900.0, DAY_ONE), []) is RiskStatus.NORMAL
    assert governor.size_multiplier() == pytest.approx(1.0)
