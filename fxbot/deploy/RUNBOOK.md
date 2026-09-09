# fxbot runbook

Everything in this file assumes you are RDP'd into the London VPS as the `fxbot` user, with
the repository at `C:\fxbot`. `PY` below means `C:\fxbot\.venv\Scripts\python.exe`.

Two things are true of every procedure here:

* **The bot never closes positions to "be safe".** Halting stops *new orders*. Open trades
  keep their server-side stops, which is the whole point of putting them there.
* **`HALTED` is cleared by a human, never by time, a restart, or a new trading day.**

---

## 1. Is it alive?

```powershell
nssm status fxbot                       # SERVICE_RUNNING
& $PY -m fxbot.cli status --env live    # risk status, equity, open tickets
Get-Content C:\fxbot\logs\fxbot.jsonl -Tail 20
```

The authoritative signal is the **heartbeat**, not the service state: a hung process still
shows as running. Configure `alerts.heartbeat_url` to a dead-man's-switch (healthchecks.io
or similar) and set it to page you after **two missed bars during market hours**. A bot that
has crashed cannot send you an alert about having crashed.

Expect one heartbeat shortly after each H1 bar close (`bar close + post_close_delay_s`).

## 2. Reading the risk status

`fxbot status` prints the persisted state. What each status means:

| Status | New entries | Open positions | What you do |
|---|---|---|---|
| `NORMAL` | yes | managed | nothing |
| `REDUCED` | yes, at half size | managed | nothing; it clears on the next broker day |
| `DAILY_LOCKOUT` | **no** | still managed | nothing; it clears on the next broker day |
| `HALTED` | **no** | stops stay on the broker; the bot sends nothing | §6 below |

`REDUCED` and `DAILY_LOCKOUT` are self-clearing. If either survives a broker-day rollover,
that is a bug — capture `state/risk_state.json` and the `risk_events` table before touching
anything.

## 3. Halting

```powershell
& $PY -m fxbot.cli kill --env live --reason "why you are doing this"
```

This writes `HALTED` to `state/risk_state.json` atomically and it survives a restart. It
does **not** close anything. The running service notices at its next cycle; to stop the
process too, `nssm stop fxbot`.

## 4. Flattening everything, by hand

```powershell
& $PY -m fxbot.cli flatten --env live --yes
```

Closes every position carrying the bot's magic number at market. Use it when you want out
regardless of price — not as a reaction to a drawdown the risk limits already handle. It
requires `--yes` because it closes real positions.

If the bot cannot reach the terminal, close the positions in MT5 by hand; the next
reconciliation adopts reality and records the closes from the deal history.

## 5. Resetting a halt

```powershell
& $PY -m fxbot.cli reset --env live --operator "your name"
```

The operator name is mandatory and recorded in `risk_events`: an unattributed reset of a
kill switch is a hole in the audit trail. **Do not reset until you have finished §6.**

## 6. `HALTED` recovery checklist

Work through this in order. Do not skip to the reset.

1. **Why?** `fxbot status --env live` prints `halted_reason`. Cross-check the
   `risk_events` table for the numbers that caused it.
2. **Max drawdown (`equity <= hwm * (1 - 10%)`).** This is the strategy losing money, not a
   fault. Do not reset the same day. Compare live expectancy in R against the backtest over
   the same window before deciding anything.
3. **Corrupt risk state.** `state/risk_state.json` failed to parse. Keep the file — it is
   evidence. Establish the true position book from the broker and the journal, then write a
   fresh state by hand and reset.
4. **Reconciliation failure (3 consecutive).** The bot's view of positions disagreed with
   the broker's three cycles running. Compare `positions_get()` in MT5 against the
   `orders`/`trades` tables. **Never restart into this**: a restart that loses state and
   then opens a second position on the same symbol is the failure this halt exists to
   prevent.
5. **`NO_MONEY` (10019).** Margin exhausted. Check `account_info()`, check for a
   withdrawal, and check whether position sizing and the broker disagree.
6. **`INVALID_VOLUME` (10014).** `sizing.py` disagrees with the broker about
   `volume_step`/`min`/`max`. Re-capture the symbol specs
   (`python -m scripts.download_history --dump-specs`) and diff them against
   `tests/fixtures/specs/`. This is a code bug until proven otherwise.
7. **`CLIENT_DISABLES_AT` (10027).** Algo Trading is off in the terminal. See §8.
8. **Three consecutive connection failures.** Terminal closed, logged out, or updating. See
   §8, then reset.

Once the cause is understood **and fixed**, reset. Then watch the first cycle in the log
before walking away.

## 7. What each alert means, and the first three things to check

| Alert | First three checks |
|---|---|
| `HALTED` transition | §6 checklist · `risk_events` · the last 50 log lines |
| `NO_MONEY` | account equity · open positions · recent withdrawals |
| reconciliation failure | `positions_get()` vs the journal · duplicate tickets · magic number |
| `trade_allowed == False` | Algo Trading button · terminal logged in · terminal updating |
| 3 connection failures | is the terminal running · is the account logged in · broker status page |
| `DAILY_LOCKOUT` | today's realised and floating P/L · was it one bad trade or five · spread at entry |
| `REDUCED` | the last three trades in `trades` · reject histogram for the week |
| order rejected | the full request in `orders` · `stops_level` · spread at send time |
| slippage > 3x median | news calendar · spread at the time · whether the entry bar gapped |
| data quality failure | `gap_count` in the log line · terminal chart history · symbol still tradeable |

## 8. Terminal problems

The terminal needs an **interactive desktop session**; it does not work as a bare service.

* **Auto-logon.** A dedicated `fxbot` Windows user with `AutoAdminLogon` set (via
  `netplwiz`). Treat that credential as a secret and keep RDP restricted to your IP.
* **Startup.** An MT5 shortcut in that user's `Startup` folder, *or* let
  `mt5.initialize(path=...)` start it. Prefer the explicit `path=` form — it removes a
  whole class of "which terminal did it attach to" bugs when several are installed.
* **Algo Trading.** Tools → Options → Expert Advisors → *Allow Algo Trading*, and the
  toolbar button must be **green**. The bot asserts `terminal_info().trade_allowed` at
  startup and refuses to run without it, so you find out at boot rather than at 3am.
* **RDP disconnect must not lock the session** — a locked session can suspend GUI apps.
  Disconnect with `tscon`, or configure the session to stay active.
* **Auto-update.** A forced update mid-session breaks the Python bridge. Patch deliberately
  at the weekend (§10).

## 9. Rolling back a release

```powershell
nssm stop fxbot
git -C C:\fxbot log --oneline -n 10
git -C C:\fxbot checkout <last-known-good-sha>
& $PY -m pip install -e C:\fxbot
& $PY -m pytest C:\fxbot\tests -q          # including test_parity.py
nssm start fxbot
& $PY -m fxbot.cli status --env live
```

`state/` is **not** rolled back and must not be: the kill switch, the equity high-water
mark and the journal describe the account, not the code. If a rollback crosses a
`risk_state.json` schema version, migrate deliberately rather than deleting the file.

## 10. Weekend patching

The FX week closes Friday evening server time and opens Sunday evening. Patch in that gap.

1. Friday after the close: `nssm stop fxbot`, confirm no open positions you are unwilling
   to hold through the weekend (the bot holds through weekends by design; stops remain on
   the broker).
2. `Install-WindowsUpdate` / reboot as needed.
3. Update MT5 if an update is pending. Log in and confirm Algo Trading is still green — an
   update can reset it.
4. `git -C C:\fxbot pull --ff-only` and re-run the tests.
5. `nssm start fxbot` well before the Sunday open, then check `fxbot status` and the first
   heartbeat.

## 11. Backups

`state/` and `logs/` sync off-VPS daily. The journal is the audit trail; losing it loses
your ability to diagnose anything. Keep a separate **read-only investor password** so you
can watch the account from your phone without carrying the trading credential.

## 12. Promotion path

Never skip a step (§13.5):

1. Backtest passes every §11.4 gate.
2. `dry_run: true` against the live feed, ≥ 2 weeks, zero unhandled exceptions.
3. DEMO on the VPS, ≥ 4 weeks, full service install. Compare demo trades to the backtest
   over the same window — entries should match within slippage. If they do not, find out
   why before risking money.
4. LIVE at 0.1% risk per trade, ≥ 4 weeks, ≥ 20 trades.
5. LIVE at 0.5% only once live expectancy in R is within one standard error of the
   backtest.

Every promotion is a `--env` change and a one-line edit to `config/live.yaml`.
