# Spec notes — Plough Backer V1

`README.md` is the authoritative spec. This file records (1) the requirements the code
is organised around and (2) open questions that a later phase must NOT resolve silently
(README §76). Nothing here amends the README.

## 1. Extracted requirements

**Invariants (§72):** INV-01…INV-15. The ones that drive the architecture:
- only a SETTLED executed trade applies a WIN/LOSS rule to progression (INV-02). Separately,
  sizing resets an at/above-maximum progression cycle to the current broker `volume_min`.
  Equity Lock blocks, broker rejections, paused/skipped/MT5-unavailable signals never apply
  trade outcomes (INV-03/04/05, §21, §34, §57);
- `theoretical_lot` (Decimal, progression-owned) ≠ `executed_lot` (broker-normalized); normalization never writes back (INV-06/07, §16–17);
- risk is computed from the broker symbol spec + SL distance, and it never gates execution. Only Equity Lock and broker impossibility can stop a trade (INV-09–12, §19);
- one Telegram message → at most one MT5 execution, including across restarts (INV-01, INV-15, §24, §27);
- MT5 history is the source of truth for realized P/L (INV-14, §29).

**Domain boundaries:**

| Layer | Owns | May not depend on |
|---|---|---|
| `signals` | NormalizedSignal, parser profiles, validation, fingerprint, symbol resolution | Telegram, MT5, DB |
| `risk` | 6 progression modes, normalize_volume, risk math, Equity Lock | Telegram, MT5, DB |
| `shadow` | 6 virtual portfolios | Telegram, MT5, DB |
| `trading/gateway.py` | `BrokerGateway` Protocol, `SymbolSpecification`, `AccountSnapshot` | MT5 package |
| `trading/*` (Phase 5+) | MT5 adapter, executor, reconciliation, settlement | Telegram |
| `persistence` | engine, `DecimalText`, ORM, repositories, migration gate | Telegram, MT5 |
| `telegram` | listener, admin bot, notifications | (none; holds no trading rules) |

Enforced by `tests/unit/test_architecture.py`.

**Lifecycle (§7):** RECEIVED → SOURCE_VALIDATED → PARSED → NORMALIZED → SYMBOL_RESOLVED →
VALIDATED → DEDUPLICATED → SIZED → EQUITY_LOCK_CHECKED → EXECUTION_REQUESTED → EXECUTED →
OPEN → CLOSED → SETTLED. Terminal: REJECTED_PARSE, REJECTED_INVALID_SIGNAL,
REJECTED_DUPLICATE, BLOCKED_EQUITY_LOCK, BROKER_REJECTED, EXECUTION_FAILED, CANCELLED,
CLOSED_WIN/LOSS/OTHER, SKIPPED_PAUSED (§34), SKIPPED_MT5_UNAVAILABLE (§57). Every transition is persisted.

**Six modes (§15):** 1 W×2 / L→base · 2 L×2 / W→base · 3 W×2 / L÷2 floored at base ·
4 ×2 on every settled trade · 5 ×2 after every 5 settled trades · 6 W×1.5 / L hold.
Settlement: BREAKEVEN → unchanged. OTHER → unchanged plus a review flag (§22).

**MT5 responsibilities (§28–30, §57):** connection, symbol select and spec, tick, retcode-validated
submission, response persistence, periodic reconciliation (positions/orders/history), restart
reconciliation, bounded reconnect. No progression logic inside the adapter (Phase 5 note).

**Telegram responsibilities (§31–35, §39–41, §54, §58):** listener with reconnect and missed-message
catch-up, admin-only bot (dashboard, mode select with confirmation, Equity Lock with confirmation,
pause/resume/stop), notifications. STOP never closes positions.

**Persistence (§42, §43, §51, §52):** SQLite with Alembic only. Journal fields per §42. Immutable
audit events per §43. Progression state with version for optimistic locking. Duplicate
fingerprints survive restarts.

**Acceptance gates:** §68 (28 demo scenarios with evidence), §69 checklist, §79 statement.
DEMO_READY ≠ live authorization.

## 2. Open questions (must be answered before the phase noted)

| ID | Blocks | Question |
|---|---|---|
| Q-01 | Phase 1 | **Scope vs base lot.** "Synthetic" means broker synthetics such as Deriv's (Volatility, Boom/Crash, Step, Jump), whose `volume_min` varies by symbol: 0.0005, 0.001, 0.5, … (owner, 2026-09-24). One shared absolute lot across symbols is therefore unusable (0.04 is invalid on a 0.5-min symbol and 80× base on a 0.0005 one). Choose: (a) an independent progression per (scope, symbol), each starting at its own `volume_min`; or (b) one progression per scope stored as a **multiple of base** (1×, 2×, 4×…), applied to each symbol's own `volume_min`. They differ: under (b) a Gold win doubles the next Boom trade; under (a) it doesn't. §52's `symbol_or_scope` hints at (a). Also: what if the broker's `volume_min` changes mid-progression (§14)? |
| Q-02 | Phase 1 | **BREAKEVEN/OTHER in modes 4 and 5.** §22 says progression is unchanged. §15 says Mode 4 doubles "after every settled trade" and Mode 5 counts "SETTLED trades". Does a BREAKEVEN/OTHER settlement double Mode 4, or count toward Mode 5's five? |
| Q-03 | Phase 8 | **Outcome classification.** Is WIN/LOSS decided by the sign of net profit (after swap and commission) or of gross profit? Is there a breakeven tolerance? Is a manual close in profit a WIN or OTHER (§29 lists manual closes)? How are partial closes handled? |
| Q-04 | Phase 4 | **TP for non-Gold symbols.** Only Gold's rule (TP2) is specified. Which TP do synthetics and Forex use? For Gold, is a lone unlabelled "TP" treated as TP1 and therefore rejected under missing_tp2_policy=REJECT? |
| Q-05 | Phase 2 | **Decided.** Before progression sizing, `theoretical_lot >= volume_max` resets the complete cycle to the symbol's current `volume_min` and emits `PROGRESSION_RESET`; the maximum is not traded. Normalization remains ROUND_HALF_UP on a zero-anchored grid, and non-progression callers retain explicit CAP/REJECT behavior. Confirm real Deriv symbol specs in Phase 5, including any separate per-symbol aggregate `volume_limit`. |
| Q-06 | Phase 2 | **Risk entry price for MARKET orders.** Use the signal's entry or the live ask/bid? "BUY GOLD NOW" has no entry at all. Is R-multiple based on planned or actual fill? |
| Q-07 | Phase 2 | **Equity Lock detail.** Block when projected == lock? (The §20 example only shows strict <.) Does projected equity subtract the SL risk of other open positions, or only this trade's? |
| Q-08 | Phase 9 | **Shadow Mode inputs.** Starting virtual balance? How are outcomes found for signals the real account didn't trade (paused, Equity Lock, broker-rejected, PAPER): price-history simulation (which data?) or skip? Is shadow P/L priced at SL/TP levels or at the real realized close (manual closes, slippage)? Which margin model decides SHADOW_FAILED (leverage source)? Does Equity Lock apply to shadows? §48 shows all six, including the active mode, as virtual portfolios. Confirm. |
| Q-09 | Phase 6 | **PAPER settlement.** How does a paper trade close with no MT5 position, and does PAPER progression advance (INV-02 says only executed trades count)? |
| Q-10 | Phase 1 | **Decimal precision.** The default 28-digit context keeps Mode 6 exact for 23 straight wins, then rounds. Proposal: 50-digit context and never quantize the theoretical lot. Confirm. |
| Q-11 | Phase 3 | **State machine gaps.** There is no state for signals arriving under the kill switch (SKIPPED_STOPPED?). Relationship between CLOSED → SETTLED and CLOSED_WIN/LOSS/OTHER. Which terminal state does BREAKEVEN get? From which states may SKIPPED_* branch? |
| Q-12 | Phase 7 | **STOP vs PAUSE.** Both mean "no new trades". Does STOP also stop listening, journaling or shadow? Do PAUSE and STOP persist across restart? Can STOP be resumed from Telegram? |
| Q-13 | Phase 4 | **Edits before execution.** A rejected or not-yet-executed message is edited into a valid signal. Should the edit be parsed and executed? (§25 covers only post-execution edits.) |
| Q-14 | Phase 4 | **Pending orders and SL sanity.** Entry ranges ("4500-4495"), pending expiry, and when a price turns a "BUY 4500" into a limit vs a market order. Is an SL on the wrong side of entry REJECTED_INVALID_SIGNAL, or left to the broker? |
| Q-15 | Phase 1/7 | **Mode change reset (§33).** In PER_SOURCE, does a change reset every scope? Is the Mode 5 counter reset? Are shadow portfolios unaffected? |
| Q-16 | Phase 7 | **Admin IDs.** §53 has a single TELEGRAM_ADMIN_USER_ID; §54 says "admin IDs". Currently a single int. |
| Q-17 | Phase 3 | **Env vs persisted settings.** Risk mode and Equity Lock can be changed from Telegram and persisted (§32, §39). Proposal: env values only seed an empty DB, then persisted values win, so a restart doesn't reset them (§30). Confirm. |
| Q-18 | Phase 4 | **Fingerprint fields (§24, "candidate").** With message_id included, the same signal re-posted as a new message is not a duplicate. Intended? |
| Q-19 | Phase 4 | **Multi-entry signals (INV-01 exception).** Assumed unsupported in V1, so rejected. Confirm. |

**Provisional choices in Phase 1 (`risk/progression.py`), pending owner confirmation:**
- Q-01: the rules don't depend on it. Each state carries its own `base_lot`, and the caller decides how states are grouped.
- Q-02: follows README §22's stated V1 default: BREAKEVEN/OTHER leave every mode unchanged, including Mode 4 and Mode 5's counter. It's a one-line change in `settle`.
- Q-10: 50-digit context, and inexact results raise instead of rounding. Mode 6 stays exact for 42 consecutive wins.

**Provisional choices in Phase 2 (`risk/normalization.py`, `engine.py`, `equity_lock.py`):**
- Q-05: `normalize_volume` requires `max_policy` (CAP | REJECT) with no default. REJECT raises a BrokerError (§21). Ties round half-up (§16 table). A `volume_min` off the step grid fails closed.
- Q-06: `assess_risk` takes `entry_price` explicitly. The orchestrator (Phase 6) picks signal entry or live tick.
- Q-07: follows §20 literally, "projected < protected blocks", so equality is allowed.
- Q-20 (new): §19 wants "HIGH EXPOSURE" warnings, but no threshold is specified. `equity_risk_percent` is computed, and the warning threshold is still needed.

**Provisional choices in Phase 3 (`persistence/`, migration 0002):**
- Q-11: every transition is persisted to `signal_events`. The allowed-transition graph is not enforced yet.
- Q-17: `seed_setting` fills an empty DB from env and never overwrites. Values set from Telegram win after a restart.
- Q-01: `progression_states.scope_key` is an opaque string. The caller builds it once Q-01 is answered.
- Deferred tables: `shadow_states` and `shadow_trades` (Phase 9), `reports` (Phase 10), `equity_locks`, `signal_sources` and `symbol_mappings` (Phase 7). Until then, YAML config and `audit_events` cover them.

**Provisional choices in Phase 4 (`signals/`):**
- Only one parser profile exists, `standard_v1`, covering the README §9 formats. Real channels each need a profile built from their real messages.
- Q-04: non-Gold TP is chosen by a per-symbol `take_profit_policy` (TP1/TP2/TP3) in symbols.yaml. If unset, the signal is rejected (`TP_POLICY_NOT_CONFIGURED`). An unlabelled "TP" never counts as TPn.
- Q-14: an entry range is rejected. A header price without LIMIT/STOP means MARKET with a reference entry. SL-side sanity checks are left to the broker for now.
- Q-18: the fingerprint uses the §24 candidate fields, message id included. Prices are normalized (4500 = 4500.00).
- Q-19: two BUY/SELL instructions in one message are rejected.

**Phase 5 (`trading/mt5_client.py`):** unit-tested in Docker against a fake MetaTrader5 module. **Not yet run against a real terminal** (Windows-only package, and Windows Python is blocked by CFA). `deviation_points` and `magic` are required constructor arguments. Partial fills report the actual filled volume.
- Q-21 (new): MT5 reports times in the trade server's timezone, and they are currently treated as UTC. The broker offset must be measured on the demo before latency and reports (§50, §47) can be trusted.

**Provisional choices in Phases 6–7 (`trading/executor.py`, `controls.py`, `telegram/`):**
- Q-01: scope key = `{scope}:{source|*}:{symbol}`, i.e. option (a), one progression per symbol. It's a one-line change in `scope_key`.
- Q-05/Q-06: `ExecutionPolicy` requires `volume_max_policy` and `risk_entry`, with no defaults.
- Q-09: PAPER sizes, risk-checks and journals the signal, then stops at `SKIPPED_PAPER`. No simulated fills or settlement.
- Q-11: new states `SKIPPED_STOPPED` (kill switch) and `SKIPPED_PAPER`.
- Q-12: STOP and PAUSE both block new orders, and both can be resumed from Telegram. Neither closes positions.
- Q-13: an edit of a non-executed message is stored only, never re-parsed. An edit after execution triggers the §25 alert only.
- Q-16: `AdminCommands` accepts a set of admin ids. Config still has a single `TELEGRAM_ADMIN_USER_ID`.
- Wired in `runtime.run()`: bot polls with `drop_pending_updates=True` and `AIORateLimiter`; the listener warms its entity cache (`get_dialogs`) and alerts about source chats it can't see; §58 catch-up runs after recovery. Windows uses the selector event loop for Telethon. Never connected to real Telegram yet (Phase 12).

**Provisional choices in Phases 8–10:**
- Q-03: `SettlementPolicy(breakeven_tolerance, manual_close_is_other)` is required. Net P/L includes swap, commission and fee. BREAKEVEN and OTHER end in `CLOSED_OTHER`.
- Q-22 (new): if the risk mode changed while a trade was open, its result is not applied to the new method. The trade is flagged for review.
- Q-08: Shadow requires a starting balance and a margin function. It uses the real signal outcome, priced at SL or TP for each portfolio's own lot. BREAKEVEN/OTHER trades are skipped. There are no shadow tables: state is recomputed from the journal.
- Reports: built from settled trades. Opening balance is the first trade's `balance_before`, and deposits are ignored. Scheduling (APScheduler) and a `reports` idempotency table are deferred to Phase 11.
- Pending-order fills and cancellations are not reconciled yet (Phase 11).

**Phases 11–12 (`runtime.py`, `resilience.py`, `scripts/demo_acceptance.py`):**
- `main` refuses to start until every open decision is set in `.env` (`runtime_missing()`).
- Scheduling uses asyncio tasks, not APScheduler: the process already runs one asyncio loop (Telethon + PTB), and two periodic jobs don't need a scheduler library. Reports are sent once per period, deduplicated via `REPORT_GENERATED` audits.
- Restart: an order claimed but unanswered by MT5 is flagged and reported, never resent. Catch-up replays only messages after the last one recorded per source; a first start never trades channel history.
- Telegram downtime is not recorded separately (§58). Telethon reconnects itself; add hooks if downtime reporting is needed.
- `runtime.run()` (real wiring) has never been executed. It needs credentials and a Windows MT5 terminal.
- Phase 12 is evidence collection on a live demo. `demo_acceptance.py` marks a scenario PASS only when the journal proves it. SL/TP closure evidence uses close price = SL/TP (the deal exit reason isn't persisted). Scenario 28 is MANUAL.

## 3. Foundation status

Implemented: typed config (env + YAML), enums, exceptions, Decimal utilities, JSON logging
with redaction, `NormalizedSignal`, `BrokerGateway` boundary, SQLite engine and `DecimalText`,
Alembic with a baseline revision, migration gate at startup, `main` bootstrap,
`scripts/health_check.py`, and tests.

Dev checks run in Docker (Python 3.12, repo mounted read-only). On this machine Windows
python.exe can't write files (Controlled Folder Access):

```powershell
docker compose build dev
docker compose run --rm dev pytest
docker compose run --rm dev ruff check .
docker compose run --rm dev ruff format --check .
docker compose run --rm dev mypy
```

The MetaTrader5 package is Windows-only, so the MT5 adapter's live tests and the demo gate
(Phases 5 and 12) need Windows Python on the MT5 machine.
