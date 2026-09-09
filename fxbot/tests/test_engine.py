"""Engine orchestration tests (§10.2, §12.3).

Managing before opening matters: a full position book must still get its trailing stops
when new entries are blocked. Reconciliation runs before anything else, and no order is
ever sent before it succeeds.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import SERVER_TZ, load_bars, load_spec

from fxbot.backtest.replay import ReplayDataSource, frame_to_bars
from fxbot.core.clock import ServerClock
from fxbot.core.enums import IntentKind, RiskStatus, Side
from fxbot.core.errors import ReconciliationError
from fxbot.core.models import Intent
from fxbot.execution.paper_broker import PaperBroker
from fxbot.ops.health import Health
from fxbot.risk.governor import RiskGovernor
from fxbot.runtime.engine import TradingEngine

WARMUP_START = 599


def build_engine(cfg, journal, alerter, tmp_path, bars=2_000):  # noqa: ANN001, ANN201
    """Wire a full engine over replay data, exactly as ``replay_history`` does."""
    frames = {"EURUSD": load_bars("parity_eurusd").iloc[:bars]}
    specs = {"EURUSD": load_spec("EURUSD")}
    clock = ServerClock(3)
    clock.observe(frames["EURUSD"].index[0].to_pydatetime())
    broker = PaperBroker(cfg, specs, {"EURUSD": frame_to_bars(frames["EURUSD"])}, 10_000.0)
    source = ReplayDataSource(frames, specs, clock, broker, cfg.timeframe_minutes)
    governor = RiskGovernor(cfg, tmp_path / "risk.json", clock, journal)
    governor.load()
    governor.set_symbol_specs(specs)
    health = Health(None, cfg.timeframe_minutes, cfg.runtime.watchdog_multiples)
    narrowed = cfg.model_copy(update={"symbols": ["EURUSD"]})
    engine = TradingEngine(narrowed, clock, source, broker, governor, journal, alerter, health)
    return engine, broker, source, governor, frames


def advance(engine, broker, source, frames, start, stop) -> None:  # noqa: ANN001
    """Drive the engine over ``[start, stop)`` the way the replay harness does."""
    for index in range(start, stop):
        for symbol in frames:
            broker.on_bar(symbol, index)
        source.seek_all(index)
        engine.run_cycle()


def advance_to_open_position(engine, broker, source, frames, magic,  # noqa: ANN001
                             start=WARMUP_START, limit=2_000) -> int:
    """Drive the engine until a position is open, and return that bar index.

    The fixture's first entry is a property of the data, not something to hardcode: a
    regenerated fixture would move it and leave the test asserting on an empty book.
    """
    for index in range(start, limit):
        for symbol in frames:
            broker.on_bar(symbol, index)
        source.seek_all(index)
        engine.run_cycle()
        if broker.positions(magic):
            return index
    raise AssertionError("the fixture never opened a position; the test data is wrong")


def test_a_cycle_runs_end_to_end_and_journals_it(cfg, journal, alerter, tmp_path) -> None:
    """One cycle records a heartbeat row and leaves the governor NORMAL."""
    engine, broker, source, governor, frames = build_engine(cfg, journal, alerter, tmp_path)
    advance(engine, broker, source, frames, WARMUP_START, WARMUP_START + 5)
    assert engine.cycles == 5
    assert governor.status is RiskStatus.NORMAL
    assert engine.health.last_cycle is not None


def test_every_decision_is_journalled_including_rejections(cfg, journal, alerter,
                                                            tmp_path) -> None:
    """The reject histogram is the primary debugging tool, so rejections are rows (§10.3)."""
    engine, broker, source, governor, frames = build_engine(cfg, journal, alerter, tmp_path)
    advance(engine, broker, source, frames, WARMUP_START, WARMUP_START + 60)
    histogram = journal.reject_histogram()
    assert histogram
    assert sum(histogram.values()) > 0
    assert set(histogram) - {"NONE"}, "a run with only successes proves nothing"


def test_daily_lockout_blocks_entries_but_not_stop_moves(cfg, journal, alerter,
                                                          tmp_path) -> None:
    """§8.5's behaviour table: no new entries, but open positions are still managed.

    The lockout is re-asserted before each cycle because a broker-day rollover inside the
    window would legitimately clear it -- that behaviour has its own test in
    ``test_governor.py`` and is not what this one is about.
    """
    engine, broker, source, governor, frames = build_engine(cfg, journal, alerter, tmp_path)
    opened = advance_to_open_position(engine, broker, source, frames, cfg.execution.magic)

    managed: list[Intent] = []
    entries: list[str] = []
    original_execute, original_entry = engine.execute, engine._consider_entry

    def execute_spy(intent, position):  # noqa: ANN001, ANN202
        managed.append(intent)
        return original_execute(intent, position)

    def entry_spy(symbol, account, positions):  # noqa: ANN001, ANN202
        entries.append(symbol)
        return original_entry(symbol, account, positions)

    engine.execute = execute_spy  # type: ignore[method-assign]
    engine._consider_entry = entry_spy  # type: ignore[method-assign]

    for index in range(opened + 1, opened + 30):
        governor.state.status = RiskStatus.DAILY_LOCKOUT
        for symbol in frames:
            broker.on_bar(symbol, index)
        source.seek_all(index)
        engine.run_cycle()

    assert entries == [], "DAILY_LOCKOUT must block every entry"
    assert managed, "management must keep running while entries are blocked"
    assert all(i.kind is not IntentKind.OPEN for i in managed)


def test_management_runs_before_entries(cfg, journal, alerter, tmp_path) -> None:
    """Manage-before-open: the position book is serviced first, every cycle."""
    engine, broker, source, governor, frames = build_engine(cfg, journal, alerter, tmp_path)
    opened = advance_to_open_position(engine, broker, source, frames, cfg.execution.magic)

    order: list[str] = []
    original_manage, original_entry = engine._manage, engine._consider_entry

    def manage_spy(position):  # noqa: ANN001, ANN202
        order.append("manage")
        return original_manage(position)

    def entry_spy(symbol, account, positions):  # noqa: ANN001, ANN202
        order.append("entry")
        return original_entry(symbol, account, positions)

    engine._manage = manage_spy  # type: ignore[method-assign]
    engine._consider_entry = entry_spy  # type: ignore[method-assign]
    advance(engine, broker, source, frames, opened + 1, opened + 25)

    assert "manage" in order
    if "entry" in order:
        assert order.index("manage") < order.index("entry")


def test_halted_sends_no_orders_at_all(cfg, journal, alerter, tmp_path) -> None:
    """In HALTED the bot sends nothing; the broker-side stops remain the backstop."""
    engine, broker, source, governor, frames = build_engine(cfg, journal, alerter, tmp_path)
    opened = advance_to_open_position(engine, broker, source, frames, cfg.execution.magic)
    governor.halt("test halt")

    sent: list[Intent] = []
    engine.execute = lambda intent, position: sent.append(intent)  # type: ignore[assignment]  # noqa: ARG005
    advance(engine, broker, source, frames, opened + 1, opened + 20)
    assert sent == []


def test_reconciliation_adopts_an_orphan_position(cfg, journal, alerter, tmp_path) -> None:
    """A broker position the journal does not know is adopted, flagged and alerted (§8.6)."""
    engine, broker, source, governor, frames = build_engine(cfg, journal, alerter, tmp_path)
    advance(engine, broker, source, frames, WARMUP_START, WARMUP_START + 3)

    from fxbot.core.models import SizedOrder

    source.seek_all(WARMUP_START + 3)
    broker.on_bar("EURUSD", WARMUP_START + 3)
    result = broker.open(SizedOrder(symbol="EURUSD", side=Side.BUY, volume=0.10,
                                    stop_price=1.05, take_profit=None, risk_amount=50.0,
                                    risk_pct=0.5, approval_id="orphan-1"))
    assert result.ok and result.ticket is not None
    engine._meta.clear()
    governor.state.open_tickets = []

    positions = engine._enrich(broker.positions(cfg.execution.magic))
    engine.reconcile(positions)

    assert result.ticket in governor.state.open_tickets
    assert engine._meta[result.ticket].initial_stop > 0.0
    assert any("orphan" in message for _, message in alerter.sent)


def test_three_failed_reconciliations_halt(cfg, journal, alerter, tmp_path) -> None:
    """§8.6 step 6. Never send an order before reconciliation succeeds."""
    engine, broker, source, governor, frames = build_engine(cfg, journal, alerter, tmp_path)

    def explode() -> dict[int, float]:
        raise OSError("journal unavailable")

    journal.open_position_stops = explode  # type: ignore[method-assign]
    governor.state.open_tickets = []
    fake = engine._enrich(broker.positions(cfg.execution.magic))
    from fxbot.core.models import SizedOrder

    source.seek_all(WARMUP_START)
    broker.on_bar("EURUSD", WARMUP_START)
    broker.open(SizedOrder(symbol="EURUSD", side=Side.BUY, volume=0.10, stop_price=1.05,
                           take_profit=None, risk_amount=50.0, risk_pct=0.5,
                           approval_id="x"))
    fake = engine._enrich(broker.positions(cfg.execution.magic))

    for _ in range(2):
        engine.reconcile(fake)
    with pytest.raises(ReconciliationError):
        engine.reconcile(fake)


def test_a_vanished_position_is_settled_from_the_deal_history(cfg, journal, alerter,
                                                               tmp_path) -> None:
    """A position that closed while the bot was down reaches the governor (§8.6 step 4)."""
    engine, broker, source, governor, frames = build_engine(cfg, journal, alerter, tmp_path)
    opened = advance_to_open_position(engine, broker, source, frames, cfg.execution.magic)
    assert broker.closed_trades == []
    advance(engine, broker, source, frames, opened + 1, 2_000)
    assert broker.closed_trades, "the fixture's positions do close"
    assert journal.recorded_tickets(), "closed trades reach the journal via reconciliation"


def test_flatten_closes_every_bot_position(cfg, journal, alerter, tmp_path) -> None:
    """``fxbot flatten`` is the runbook's emergency exit."""
    engine, broker, source, governor, frames = build_engine(cfg, journal, alerter, tmp_path)
    advance_to_open_position(engine, broker, source, frames, cfg.execution.magic)
    open_before = len(broker.positions(cfg.execution.magic))
    assert open_before > 0
    assert engine.flatten("manual") == open_before
    assert broker.positions(cfg.execution.magic) == []


def test_the_context_never_includes_the_forming_bar(cfg, journal, alerter, tmp_path) -> None:
    """Closed bars only (§0.3): the context's last bar is the one at the cursor."""
    engine, broker, source, governor, frames = build_engine(cfg, journal, alerter, tmp_path)
    source.seek_all(700)
    ctx = engine.build_context("EURUSD")
    assert ctx is not None
    assert ctx.h1.index[-1] == frames["EURUSD"].index[700]
    assert ctx.now == frames["EURUSD"].index[700].to_pydatetime() + timedelta(hours=1)
    assert ctx.d1.index[-1].date() < ctx.now.date()


def test_a_quality_failure_skips_the_symbol_for_entries_only(cfg, journal, alerter,
                                                              tmp_path) -> None:
    """Strict context blocks entries; the lenient one still carries a position (§10.2)."""
    engine, broker, source, governor, frames = build_engine(cfg, journal, alerter, tmp_path)
    source.seek_all(50)
    assert engine.build_context("EURUSD") is None
    lenient = engine.build_context("EURUSD", strict=False)
    assert lenient is not None
    assert lenient.quality.ok is False


# ---------------------------------------------------------------- the live broker

def build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path):  # noqa: ANN001, ANN201
    """Wire an :class:`MT5Broker` against :class:`FakeMT5` with one tradeable symbol."""
    from types import SimpleNamespace

    from tests.conftest import symbol_info_tuple

    import fxbot.data.mt5_source as source_module
    import fxbot.execution.mt5_broker as broker_module
    from fxbot.data.mt5_source import MT5DataSource
    from fxbot.execution.filling import FillingCache
    from fxbot.execution.mt5_broker import MT5Broker

    source_module.mt5 = fake_mt5
    broker_module.mt5 = fake_mt5
    fake_mt5.infos["EURUSD"] = symbol_info_tuple(eurusd)
    fake_mt5.ticks["EURUSD"] = SimpleNamespace(bid=1.08000, ask=1.08010,
                                               time=1_717_400_000)

    clock = ServerClock(3)
    # MT5 epoch fields render the SERVER's wall clock when read as UTC, so the clock is
    # seeded through the same conversion the adapter uses (see server_time_from_epoch).
    from fxbot.data.mt5_source import server_time_from_epoch

    clock.observe(server_time_from_epoch(1_717_400_000, clock))
    source = MT5DataSource(cfg, clock)
    source.connect({})
    cache = FillingCache(tmp_path / "filling.json", "Pepperstone-Demo")
    return MT5Broker(cfg, source, clock, cache, sleep=lambda _s: None), cache


def approved_order(volume=0.23):  # noqa: ANN001, ANN201
    """A governor-approved order ready to send.

    0.23 lots is what ``position_size`` returns for the FakeMT5 account: equity $10,000,
    risk 0.5%, fill at the ask 1.08010, stop 1.07800, Razor commission. The broker
    re-sizes from a fresh tick before sending and aborts on a mismatch (§9.2 step 4), so
    the default has to be the number the risk layer actually produces.
    """
    from fxbot.core.models import SizedOrder

    return SizedOrder(symbol="EURUSD", side=Side.BUY, volume=volume, stop_price=1.07800,
                      take_profit=None, risk_amount=50.0, risk_pct=0.5,
                      approval_id="abcd1234-0000-0000-0000-000000000000")


def test_the_broker_refuses_an_order_without_an_approval(cfg, fake_mt5, eurusd,
                                                          tmp_path) -> None:
    """There is no code path that sends an order without an approval object (§0.4)."""
    from dataclasses import replace

    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    with pytest.raises(ValueError, match="approval_id"):
        broker.open(replace(approved_order(), approval_id=""))


def test_idempotency_prevents_a_double_fill_on_retry(cfg, fake_mt5, eurusd,
                                                     tmp_path) -> None:
    """A resend after an ``order_send`` timeout must not double the position (§9.5)."""
    from types import SimpleNamespace

    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    order = approved_order()

    first = broker.open(order)
    assert first.ok and first.retcode == 10009
    assert len(fake_mt5.requests) == 1

    # The order really did reach the market; history now shows it.
    fake_mt5.orders = [SimpleNamespace(magic=cfg.execution.magic, symbol="EURUSD",
                                       comment="fxbot|abcd1234",
                                       time_setup=1_717_400_000)]
    second = broker.open(order)
    assert not second.ok
    assert "idempotency" in second.comment
    assert len(fake_mt5.requests) == 1, "no second request was sent"


def test_the_authoritative_check_does_not_depend_on_the_comment(cfg, fake_mt5, eurusd,
                                                                 tmp_path) -> None:
    """Brokers rewrite comments; history_orders_get on magic + symbol is the authority."""
    from types import SimpleNamespace

    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    fake_mt5.orders = [SimpleNamespace(magic=cfg.execution.magic, symbol="EURUSD",
                                       comment="[sl 1.07800]", time_setup=1_717_400_000)]
    result = broker.open(approved_order())
    assert not result.ok
    assert fake_mt5.requests == []


def test_ten_thousand_thirty_advances_to_the_next_filling_candidate(cfg, fake_mt5, eurusd,
                                                                    tmp_path) -> None:
    """Retcode 10030 retries with the next candidate and caches the winner (§9.3)."""
    from dataclasses import replace
    from types import SimpleNamespace

    from tests.conftest import symbol_info_tuple

    from fxbot.execution.filling import ORDER_FILLING_FOK, SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC

    broker, cache = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    fake_mt5.infos["EURUSD"] = symbol_info_tuple(
        replace(eurusd, filling_modes=SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC))
    fake_mt5.send_results = [
        SimpleNamespace(retcode=10030, order=0, price=0.0, volume=0.0, comment="bad fill"),
        SimpleNamespace(retcode=10009, order=42, price=1.08010, volume=0.23, comment="done"),
    ]
    result = broker.open(approved_order())
    assert result.ok
    assert len(fake_mt5.requests) == 2
    assert fake_mt5.requests[0]["type_filling"] != fake_mt5.requests[1]["type_filling"]
    assert fake_mt5.requests[1]["type_filling"] == ORDER_FILLING_FOK
    assert cache.get("EURUSD") == ORDER_FILLING_FOK


def test_a_stale_tick_aborts_before_sending(cfg, fake_mt5, eurusd, tmp_path) -> None:
    """§9.2 step 1: never send against a dead quote."""
    from types import SimpleNamespace

    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    fake_mt5.ticks["EURUSD"] = SimpleNamespace(bid=1.08000, ask=1.08010,
                                               time=int(1_717_400_000 - 60))
    result = broker.open(approved_order())
    assert not result.ok
    assert "old" in result.comment
    assert fake_mt5.requests == []


def test_a_blown_out_spread_aborts_before_sending(cfg, fake_mt5, eurusd, tmp_path) -> None:
    """§9.2 step 2: spreads blow out between the signal and the send."""
    from types import SimpleNamespace

    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    fake_mt5.ticks["EURUSD"] = SimpleNamespace(bid=1.08000, ask=1.08500,
                                               time=1_717_400_000)
    result = broker.open(approved_order())
    assert not result.ok and "spread" in result.comment
    assert fake_mt5.requests == []


def test_a_resize_disagreement_aborts_rather_than_sending_an_unapproved_size(
        cfg, fake_mt5, eurusd, tmp_path) -> None:
    """§9.2 step 4: never send a size the governor did not approve."""
    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    result = broker.open(approved_order(volume=5.00))
    assert not result.ok
    assert "re-size" in result.comment
    assert fake_mt5.requests == []


def test_order_check_failure_aborts_without_touching_the_market(cfg, fake_mt5, eurusd,
                                                                tmp_path) -> None:
    """§9.2 step 5 catches margin, filling and stop-level errors before order_send."""
    from types import SimpleNamespace

    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    fake_mt5.check_result = SimpleNamespace(retcode=10019, comment="No money")
    result = broker.open(approved_order())
    assert not result.ok
    assert result.retcode == 10019
    assert fake_mt5.requests == [], "order_check runs before order_send"


def test_algo_trading_disabled_is_refused_at_connect(cfg, fake_mt5, eurusd,
                                                     tmp_path) -> None:
    """Assert ``terminal_info().trade_allowed`` at startup, not at 3am (§13.3)."""
    import fxbot.data.mt5_source as source_module
    from fxbot.core.errors import BrokerConnectionError
    from fxbot.data.mt5_source import MT5DataSource

    source_module.mt5 = fake_mt5
    fake_mt5.trade_allowed = False
    with pytest.raises(BrokerConnectionError, match="Algo Trading"):
        MT5DataSource(cfg, ServerClock(3)).connect({})


def test_symbol_resolution_never_hardcodes_a_name(cfg, fake_mt5, eurusd, tmp_path) -> None:
    """Suffixed broker symbols resolve; ambiguity raises rather than guessing (§6.2)."""
    from types import SimpleNamespace

    import fxbot.data.mt5_source as source_module
    from fxbot.core.errors import SymbolResolutionError
    from fxbot.data.mt5_source import MT5DataSource

    source_module.mt5 = fake_mt5
    fake_mt5.symbols = [SimpleNamespace(name="EURUSD.r", trade_mode=4),
                        SimpleNamespace(name="GBPUSD", trade_mode=4)]
    source = MT5DataSource(cfg, ServerClock(3))
    resolved = source.resolve_symbols(["EURUSD", "GBPUSD"])
    assert resolved == {"EURUSD": "EURUSD.r", "GBPUSD": "GBPUSD"}

    fake_mt5.symbols.append(SimpleNamespace(name="EURUSD.c", trade_mode=4))
    with pytest.raises(SymbolResolutionError, match="ambiguous"):
        source.resolve_symbols(["EURUSD"])

    fake_mt5.symbols = [SimpleNamespace(name="GBPUSD", trade_mode=4)]
    with pytest.raises(SymbolResolutionError, match="no broker symbol"):
        source.resolve_symbols(["EURUSD"])


# ---------------------------------------------------------------- the scheduler

def build_scheduler(cfg, journal, alerter, tmp_path, utc_times):  # noqa: ANN001, ANN201
    """Wire a scheduler over a stub engine with an injected, frozen UTC clock."""
    from fxbot.runtime.scheduler import Scheduler

    class StubEngine:
        def __init__(self) -> None:
            self.cycles = 0
            self.raise_next: Exception | None = None

        def run_cycle(self) -> None:
            self.cycles += 1
            if self.raise_next is not None:
                error, self.raise_next = self.raise_next, None
                raise error

    clock = ServerClock(3)
    engine = StubEngine()
    governor = RiskGovernor(cfg, tmp_path / "risk.json", clock, journal)
    governor.load()
    ticks = iter(utc_times)
    last = utc_times[-1]

    def fake_utc_now():  # noqa: ANN202
        nonlocal last
        with contextlib.suppress(StopIteration):
            last = next(ticks)
        return last

    scheduler = Scheduler(cfg, clock, engine, governor, alerter, fake_utc_now,
                          sleep=lambda _s: None)
    return scheduler, engine, governor


def utc_series(count: int):  # noqa: ANN201
    """A tz-aware UTC series advancing an hour at a time."""

    start = datetime(2024, 6, 3, 6, 0, tzinfo=UTC)
    return [start + timedelta(hours=i) for i in range(count)]


def test_the_scheduler_wakes_after_the_bar_close(cfg, journal, alerter, tmp_path) -> None:
    """``bar close + post_close_delay_s`` so the bar is final on the server (§10.1)."""
    scheduler, _, _ = build_scheduler(cfg, journal, alerter, tmp_path, utc_series(4))
    now = datetime(2024, 6, 3, 12, 17, tzinfo=SERVER_TZ)
    wake = scheduler.next_wake(now)
    assert wake == datetime(2024, 6, 3, 13, 0, tzinfo=SERVER_TZ) + timedelta(
        seconds=cfg.runtime.post_close_delay_s)


def test_the_scheduler_runs_the_requested_number_of_cycles(cfg, journal, alerter,
                                                            tmp_path) -> None:
    """``max_cycles`` is what the dry-run soak and the tests use to bound the loop."""
    scheduler, engine, _ = build_scheduler(cfg, journal, alerter, tmp_path, utc_series(200))
    assert scheduler.run_forever(max_cycles=3) == 3
    assert engine.cycles == 3


def test_the_loop_survives_a_bad_bar_but_says_so(cfg, journal, alerter, tmp_path) -> None:
    """One failed cycle must not stop the bot, and must not be silent either (§17.18)."""
    scheduler, engine, governor = build_scheduler(cfg, journal, alerter, tmp_path,
                                                  utc_series(200))
    engine.raise_next = RuntimeError("one bad bar")
    assert scheduler.run_forever(max_cycles=3) == 3
    assert engine.cycles == 3
    assert any("cycle failed" in message for _, message in alerter.sent)
    assert governor.status is not RiskStatus.HALTED


def test_a_fatal_error_halts_and_leaves_the_loop(cfg, journal, alerter, tmp_path) -> None:
    """§10.1: a FatalError halts the governor, alerts at CRITICAL and breaks the loop."""
    from fxbot.core.errors import ReconciliationError

    scheduler, engine, governor = build_scheduler(cfg, journal, alerter, tmp_path,
                                                  utc_series(200))
    engine.raise_next = ReconciliationError("positions disagree")
    assert scheduler.run_forever(max_cycles=5) == 1
    assert governor.status is RiskStatus.HALTED
    assert "positions disagree" in governor.state.halted_reason
    assert any(str(severity) == "Severity.CRITICAL" or "HALTED" in message
               for severity, message in alerter.sent)


def test_a_shutdown_request_ends_the_loop_and_saves_state(cfg, journal, alerter,
                                                           tmp_path) -> None:
    """Never kill mid-order: the flag is honoured between cycles, and state is persisted."""
    scheduler, engine, governor = build_scheduler(cfg, journal, alerter, tmp_path,
                                                  utc_series(200))
    scheduler._on_signal(15, None)
    assert scheduler.shutdown_requested
    assert scheduler.run_forever(max_cycles=5) == 0
    assert engine.cycles == 0
    assert (tmp_path / "risk.json").is_file()


def test_signal_handlers_install_without_raising(cfg, journal, alerter, tmp_path) -> None:
    """The service stop path has to be wired before the loop starts (§13.4)."""
    scheduler, _, _ = build_scheduler(cfg, journal, alerter, tmp_path, utc_series(4))
    scheduler.install_signal_handlers()
    assert scheduler.shutdown_requested is False


# ---------------------------------------------------------------- broker round trips

def open_position(fake_mt5, cfg, ticket=7, side="BUY", volume=0.23, comment="fxbot|abcd1234"):  # noqa: ANN001, ANN201
    """Put an open position on the FakeMT5 book."""
    from types import SimpleNamespace

    fake_mt5.positions = [SimpleNamespace(
        ticket=ticket, symbol="EURUSD",
        type=fake_mt5.POSITION_TYPE_BUY if side == "BUY" else fake_mt5.POSITION_TYPE_SELL,
        volume=volume, price_open=1.08000, sl=1.07800, tp=0.0, time=1_717_400_000,
        profit=12.5, magic=cfg.execution.magic, comment=comment)]
    return fake_mt5.positions[0]


def test_positions_are_filtered_on_magic_in_python(cfg, fake_mt5, eurusd, tmp_path) -> None:
    """``positions_get`` has no magic parameter, so the filter happens here (§8.6)."""
    from types import SimpleNamespace

    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    open_position(fake_mt5, cfg)
    fake_mt5.positions.append(SimpleNamespace(
        ticket=99, symbol="EURUSD", type=fake_mt5.POSITION_TYPE_SELL, volume=1.0,
        price_open=1.09, sl=1.10, tp=0.0, time=1_717_400_000, profit=-3.0,
        magic=123456, comment="someone else's EA"))

    ours = broker.positions(cfg.execution.magic)
    assert [p.ticket for p in ours] == [7]
    assert ours[0].side is Side.BUY
    assert ours[0].symbol == "EURUSD"


def test_closing_a_position_sends_the_opposite_side(cfg, fake_mt5, eurusd, tmp_path) -> None:
    """Closing a long sells; closing a short buys at the ask."""
    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    open_position(fake_mt5, cfg)
    result = broker.close(7, None, "tp1")
    assert result.ok
    request = fake_mt5.requests[-1]
    assert request["type"] == fake_mt5.ORDER_TYPE_SELL
    assert request["position"] == 7
    assert request["volume"] == pytest.approx(0.23)

    open_position(fake_mt5, cfg, side="SELL")
    broker.close(7, None, "bias_flip")
    assert fake_mt5.requests[-1]["type"] == fake_mt5.ORDER_TYPE_BUY


def test_a_partial_close_rounds_to_the_volume_step(cfg, fake_mt5, eurusd, tmp_path) -> None:
    """The strategy and the broker must agree on the banked volume to the last decimal."""
    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    open_position(fake_mt5, cfg, volume=0.23)
    broker.close(7, 0.115, "tp1")
    assert fake_mt5.requests[-1]["volume"] == pytest.approx(0.11)


def test_closing_an_unknown_ticket_fails_cleanly(cfg, fake_mt5, eurusd, tmp_path) -> None:
    """Failure path: no position, no order, no exception."""
    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    result = broker.close(404)
    assert not result.ok
    assert "404" in result.comment
    assert fake_mt5.requests == []

    modify = broker.modify_stop(404, 1.07, None)
    assert not modify.ok


def test_a_close_that_rounds_to_zero_is_refused(cfg, fake_mt5, eurusd, tmp_path) -> None:
    """A partial smaller than one step is not an order worth sending."""
    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    open_position(fake_mt5, cfg)
    result = broker.close(7, 0.004, "tp1")
    assert not result.ok
    assert "zero" in result.comment


def test_modify_stop_uses_the_sltp_action(cfg, fake_mt5, eurusd, tmp_path) -> None:
    """Moving a stop is TRADE_ACTION_SLTP, not a new deal."""
    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    open_position(fake_mt5, cfg)
    result = broker.modify_stop(7, 1.07950, None)
    assert result.ok
    request = fake_mt5.requests[-1]
    assert request["action"] == fake_mt5.TRADE_ACTION_SLTP
    assert request["sl"] == pytest.approx(1.07950)
    assert request["tp"] == 0.0, "no server-side take-profit in v1 (§7.4)"


def test_closed_deals_are_reconstructed_from_the_deal_history(cfg, fake_mt5, eurusd,
                                                              tmp_path) -> None:
    """A position that closed while the bot was down is found here (§8.6 step 4)."""
    from types import SimpleNamespace

    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    magic = cfg.execution.magic
    fake_mt5.deals = [
        SimpleNamespace(position_id=7, magic=magic, symbol="EURUSD", entry=0,
                        type=fake_mt5.DEAL_TYPE_BUY, volume=0.23, price=1.08000,
                        profit=0.0, commission=-0.81, swap=0.0, time=1_717_400_000,
                        comment="fxbot|abcd1234"),
        SimpleNamespace(position_id=7, magic=magic, symbol="EURUSD", entry=1,
                        type=fake_mt5.DEAL_TYPE_SELL, volume=0.23, price=1.08400,
                        profit=92.0, commission=-0.81, swap=-1.2, time=1_717_410_000,
                        comment="[sl 1.08400]"),
        SimpleNamespace(position_id=8, magic=999, symbol="EURUSD", entry=1,
                        type=fake_mt5.DEAL_TYPE_SELL, volume=1.0, price=1.08,
                        profit=5.0, commission=0.0, swap=0.0, time=1_717_410_000,
                        comment="not ours"),
    ]
    trades = broker.closed_deals(datetime(2024, 6, 1, tzinfo=SERVER_TZ), magic)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.ticket == 7
    assert trade.side is Side.BUY
    assert trade.exit_price == pytest.approx(1.08400)
    assert trade.exit_reason == "stop"
    assert trade.net_pnl == pytest.approx(92.0 - 1.62 - 1.2)


def test_the_margin_gate_asks_the_broker(cfg, fake_mt5, eurusd, tmp_path) -> None:
    """Never compute margin from leverage: the broker's number is authoritative (§8.3)."""
    from fxbot.core.errors import BrokerConnectionError

    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    assert broker.order_calc_margin(approved_order()) == pytest.approx(50.0)

    fake_mt5.margin_result = None
    with pytest.raises(BrokerConnectionError):
        broker.order_calc_margin(approved_order())


def test_a_lost_connection_is_reported_not_swallowed(cfg, fake_mt5, eurusd,
                                                     tmp_path) -> None:
    """``terminal_info()`` returning None means the terminal went away."""
    from fxbot.core.errors import BrokerConnectionError

    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    broker.ensure_connected()
    fake_mt5.terminal_info = lambda: None  # type: ignore[method-assign]
    with pytest.raises(BrokerConnectionError):
        broker.ensure_connected()


def test_the_order_comment_stays_within_mt5s_limit(cfg, fake_mt5, eurusd, tmp_path) -> None:
    """MT5 truncates past 31 characters, and non-ASCII can be rewritten entirely (§9.2)."""
    broker, _ = build_mt5_broker(cfg, fake_mt5, eurusd, tmp_path)
    broker.open(approved_order())
    comment = fake_mt5.requests[-1]["comment"]
    assert len(comment) <= 31
    assert comment.isascii()
    assert comment.startswith(cfg.execution.order_comment_prefix)


def test_the_data_source_reports_missing_metatrader_clearly(cfg) -> None:
    """On Linux there is no wheel; say so rather than failing with an ImportError."""
    import fxbot.data.mt5_source as source_module
    from fxbot.core.errors import BrokerConnectionError

    original = source_module.mt5
    source_module.mt5 = None
    try:
        with pytest.raises(BrokerConnectionError, match="Windows"):
            source_module.require_mt5()
    finally:
        source_module.mt5 = original
