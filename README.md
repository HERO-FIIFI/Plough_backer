# 🚜 Plough Backer

**Deterministic Telegram → MetaTrader 5 Signal Execution, Lot Progression, Shadow Simulation & Performance Analytics Engine**

> **Version:** 1.0.0-demo
> **Status:** V1 Implementation Specification
> **Primary Runtime:** Python 3.12+
> **Trading Terminal:** MetaTrader 5
> **Signal Source:** Telegram
> **Initial Deployment Target:** Demo-account deployment
> **Execution Philosophy:** Deterministic, auditable, fail-closed on malformed signals, no AI-dependent trading decisions

---

# 1. Overview

Plough Backer is a lightweight but production-engineered Telegram-to-MetaTrader 5 trade execution system.

Its primary responsibility is to:

1. monitor configured Telegram signal channels;
2. identify supported trading signals;
3. deterministically parse those signals;
4. normalize symbols against the connected MT5 broker;
5. select the correct take-profit target;
6. determine the next position size according to one of six selectable lot-progression methods;
7. calculate the monetary risk implied by the signal's stop loss;
8. enforce Equity Lock when enabled;
9. submit the trade to MetaTrader 5;
10. track the resulting position through closure;
11. update the active progression method using the realized outcome;
12. maintain a complete local trading journal;
13. simulate all six progression methods in Shadow Mode;
14. provide Telegram-based monitoring and controls;
15. generate weekly and monthly performance reports.

Plough Backer is intentionally simpler than a generalized trading automation platform.

The core architecture should remain:

```text
Telegram
   ↓
Signal Listener
   ↓
Deterministic Parser
   ↓
Signal Validator
   ↓
Symbol Resolver
   ↓
TP Selection
   ↓
Risk / Lot Progression Engine
   ↓
Equity Lock
   ↓
MT5 Execution Engine
   ↓
Position Reconciliation
   ↓
Journal / Analytics
   ↓
Telegram Reporting
```

The application MUST NOT use an LLM, machine-learning model, sentiment engine, or discretionary AI decision to determine whether a valid trading signal should be executed.

Execution decisions must be reproducible from persisted state and configuration.

---

# 2. Core Design Principles

## 2.1 Deterministic execution

Given identical:

* signal;
* configuration;
* MT5 symbol specification;
* progression state;
* account state;

Plough Backer should produce the same execution decision.

---

## 2.2 Lot-based progression

The six progression methods operate on **position volume / lot size**, not a fixed monetary risk amount.

The system MUST NOT assume:

```text
0.01 lot = $8 risk
```

or any other fixed monetary relationship.

Instead:

```text
Broker Minimum Lot
        +
Signal Entry
        +
Signal Stop Loss
        +
Symbol Contract/Tick Specification
        ↓
Actual Monetary Risk
```

The minimum broker-supported lot is the base lot.

Example:

```text
XAUUSD

volume_min  = 0.01
volume_step = 0.01

Base lot = 0.01
```

Another instrument may report:

```text
volume_min = 0.10
```

Its base lot is therefore:

```text
0.10
```

No symbol should be globally hard-coded to 0.01.

---

# 3. V1 Scope

V1 MUST support:

* Telegram signal ingestion;
* configurable Telegram signal sources;
* XAUUSD / Gold;
* broker-supported synthetic instruments;
* Forex/CFD symbols where parsable;
* MT5 demo accounts;
* market orders;
* pending orders where clearly represented by the signal;
* stop loss;
* multiple TP parsing;
* automatic TP2 selection for Gold;
* six lot-progression methods;
* accumulated theoretical sizing;
* broker-volume normalization;
* actual dollar-risk calculation;
* percentage-of-equity exposure calculation;
* Equity Lock;
* duplicate-signal protection;
* paper mode;
* demo mode;
* live mode architecture, but V1 acceptance MUST occur on demo first;
* Shadow Mode;
* trade journaling;
* Telegram dashboard;
* weekly reporting;
* monthly reporting;
* pause/resume;
* kill switch;
* position reconciliation;
* application restart recovery;
* structured logging;
* automated tests.

---

# 4. Explicit Non-Goals for V1

Do NOT unnecessarily expand V1.

V1 does not require:

* React;
* Next.js;
* external web dashboard;
* PostgreSQL;
* Redis;
* Celery;
* Kafka;
* Kubernetes;
* microservices;
* RAG;
* LLM trading decisions;
* autonomous strategy generation;
* sentiment analysis;
* signal prediction;
* copying arbitrary unstructured conversations;
* portfolio optimization;
* social trading;
* multi-user SaaS;
* billing;
* copy-trading marketplace.

Use Python and SQLite unless a demonstrated V1 requirement requires otherwise.

---

# 5. Technology Stack

Recommended:

```text
Python              3.12+
Telegram Listener   Telethon
Telegram Bot        python-telegram-bot
Trading             MetaTrader5 Python package
Database            SQLite
ORM                  SQLAlchemy 2.x
Migrations           Alembic
Configuration        Pydantic Settings
Scheduling           APScheduler
Testing              pytest
Logging              Python structured logging
```

A coding agent may substitute libraries only when there is a concrete technical reason.

Trading semantics MUST NOT change as a consequence.

---

# 6. Operating Modes

Plough Backer MUST expose three execution modes.

## PAPER

Signals are parsed and processed normally.

No MT5 order is submitted.

Everything else operates:

* parsing;
* progression;
* simulated execution;
* shadow modes;
* analytics;
* reporting.

---

## DEMO

Orders are submitted to a configured MT5 demo account.

This is the required V1 deployment target.

---

## LIVE

Architecture may support live accounts, but V1 MUST NOT be considered live-ready merely because demo execution succeeds.

Live mode should require an explicit configuration value.

Example:

```env
EXECUTION_MODE=DEMO
```

Never infer LIVE mode.

---

# 7. Signal Lifecycle

Every received Telegram message passes through:

```text
RECEIVED
   ↓
SOURCE_VALIDATED
   ↓
PARSED
   ↓
NORMALIZED
   ↓
SYMBOL_RESOLVED
   ↓
VALIDATED
   ↓
DEDUPLICATED
   ↓
SIZED
   ↓
EQUITY_LOCK_CHECKED
   ↓
EXECUTION_REQUESTED
   ↓
EXECUTED
   ↓
OPEN
   ↓
CLOSED
   ↓
SETTLED
```

Possible terminal states include:

```text
REJECTED_PARSE
REJECTED_INVALID_SIGNAL
REJECTED_DUPLICATE
BLOCKED_EQUITY_LOCK
BROKER_REJECTED
EXECUTION_FAILED
CANCELLED
CLOSED_WIN
CLOSED_LOSS
CLOSED_OTHER
```

State transitions MUST be persisted.

---

# 8. Telegram Signal Model

Normalize all supported messages into:

```python
NormalizedSignal(
    signal_id: str,
    source_id: str,
    source_message_id: int,
    source_timestamp: datetime,

    symbol_raw: str,
    symbol_mt5: str | None,

    direction: BUY | SELL,

    order_type: MARKET | BUY_LIMIT | SELL_LIMIT | BUY_STOP | SELL_STOP,

    entry: Decimal | None,
    stop_loss: Decimal,

    take_profits: list[Decimal],
    selected_take_profit: Decimal,

    parser_version: str,

    raw_message: str
)
```

Never execute directly from raw Telegram text.

---

# 9. Parser Philosophy

The parser MUST be deterministic.

It may recognize formats such as:

```text
BUY GOLD 4500
SL 4492
TP1 4506
TP2 4512
TP3 4518
```

or:

```text
XAUUSD BUY @ 4500

SL: 4492
TP1: 4506
TP2: 4512
```

or supported synthetic formats.

The parser should use:

* explicit regex;
* source-specific parser profiles;
* symbol aliases;
* deterministic normalization.

It MUST NOT use an LLM to guess missing trade parameters.

---

# 10. Fail-Closed Parsing

If a required execution parameter cannot be determined reliably:

```text
DO NOT TRADE
```

Example:

```text
GOLD BUY NOW
TP 4510
```

with no SL when an SL is required.

Result:

```text
REJECTED_INVALID_SIGNAL
Reason: STOP_LOSS_MISSING
```

Telegram notification:

```text
⚠️ SIGNAL NOT EXECUTED

Source: Gold Signals
Symbol: XAUUSD
Reason: Stop loss could not be determined.

Message recorded for review.
```

This is not a risk-method rejection.

It is an invalid trading instruction.

---

# 11. Symbol Resolution

Telegram symbol names frequently differ from MT5 broker symbols.

Examples:

```text
GOLD
XAUUSD
XAUUSDm
XAUUSD.
GOLD.pro
```

Plough Backer MUST maintain aliases.

Example:

```yaml
symbols:
  GOLD:
    aliases:
      - GOLD
      - XAUUSD
    mt5_symbol: XAUUSDm
```

Synthetic symbols must use the same mechanism.

Do not hard-code one broker's symbol naming convention into trading logic.

---

# 12. Gold TP Rule

For Gold/XAUUSD signals:

> **TP2 is the default execution target.**

Example:

```text
TP1 = 4510
TP2 = 4518
TP3 = 4525
```

Plough Backer executes:

```text
TP = 4518
```

TP1 and TP3 remain stored in the journal.

If TP2 is missing, V1 SHOULD reject the signal rather than silently substitute another TP unless an explicit fallback policy has been configured.

Example:

```yaml
gold:
  take_profit_policy: TP2
  missing_tp2_policy: REJECT
```

---

# 13. Synthetic Instruments

Plough Backer MUST be capable of executing synthetic instruments supported by the connected MT5 broker.

Do not assume:

* Gold contract sizes;
* Gold tick values;
* Gold minimum lots;
* Forex pip conventions.

Always retrieve symbol specifications from MT5.

Important fields include:

```text
volume_min
volume_max
volume_step
trade_tick_size
trade_tick_value
trade_contract_size
digits
point
trade_stops_level
trade_mode
```

Where possible, use MT5-native profit/loss calculation facilities rather than manually assuming contract behavior.

---

# 14. Base Lot

For every symbol:

```text
base_lot = MT5 symbol.volume_min
```

Example:

```text
XAUUSD
minimum = 0.01

base = 0.01
```

Example synthetic:

```text
SyntheticABC
minimum = 0.10

base = 0.10
```

The base must be persisted with each trade because broker specifications may later change.

---

# 15. The Six Plough-Back Modes

These definitions are authoritative.

Do not reinterpret them.

---

## Mode 1 — Anti-Martingale

```text
WIN  → next theoretical lot × 2
LOSS → reset next theoretical lot to base lot
```

Example:

```text
Base = 0.01

Trade 1 = 0.01 → WIN
Trade 2 = 0.02 → WIN
Trade 3 = 0.04 → WIN
Trade 4 = 0.08 → LOSS
Trade 5 = 0.01
```

---

## Mode 2 — Martingale

```text
LOSS → next theoretical lot × 2
WIN  → reset next theoretical lot to base lot
```

Example:

```text
0.01 LOSS
0.02 LOSS
0.04 WIN
0.01 ...
```

V1 uses the straightforward progression above.

Do not introduce additional recovery mathematics unless explicitly specified in a later version.

---

## Mode 3 — Win ×2 / Loss ÷2

```text
WIN  → next theoretical lot × 2
LOSS → next theoretical lot ÷ 2
```

The theoretical value MUST NOT fall below the symbol's base lot.

Therefore:

```text
next = max(theoretical / 2, base_lot)
```

---

## Mode 4 — Always Double

After every settled trade:

```text
next theoretical lot = previous theoretical lot × 2
```

Result does not matter.

```text
WIN  → ×2
LOSS → ×2
```

Example:

```text
0.01
0.02
0.04
0.08
0.16
...
```

This method is intentionally aggressive.

Plough Backer MUST NOT silently modify its semantics.

---

## Mode 5 — Double Every Five Completed Trades

Keep the same theoretical lot for five settled trades.

Then double it.

Example:

```text
Trades 1–5     0.01
Trades 6–10    0.02
Trades 11–15   0.04
Trades 16–20   0.08
Trades 21–25   0.16
Trades 26–30   0.32
```

Win/loss outcomes do not affect the progression.

Only SETTLED trades increment the progression counter.

---

## Mode 6 — Win +50% / Loss Hold

```text
WIN  → theoretical lot × 1.5
LOSS → theoretical lot unchanged
```

Example:

```text
Base: 0.010000

WIN  → 0.015000
WIN  → 0.022500
LOSS → 0.022500
WIN  → 0.033750
```

The theoretical value MUST retain sufficient decimal precision.

---

# 16. Accumulated Theoretical Sizing

This is a critical architectural requirement.

Maintain two different values:

```text
theoretical_lot
executed_lot
```

The progression engine operates ONLY on:

```text
theoretical_lot
```

The MT5 adapter converts it into:

```text
executed_lot
```

Example Mode 6:

```text
Broker step: 0.01

Theoretical    Executable
0.010000       0.01
0.015000       0.02
0.022500       0.02
0.033750       0.03
0.050625       0.05
```

Rounding an MT5 order MUST NOT destroy the underlying theoretical progression.

Use `Decimal`, not binary floating-point arithmetic, for progression calculations.

---

# 17. Lot Normalization

Create one centralized function:

```python
normalize_volume(
    theoretical_volume,
    volume_min,
    volume_max,
    volume_step
) -> Decimal
```

The normalization policy MUST be deterministic.

Default:

> Round theoretical volume to the nearest valid broker volume step.

It must never produce:

```text
volume < volume_min
```

and never submit a broker-invalid lot increment.

If theoretical volume exceeds `volume_max`, the broker constraint must be handled explicitly and logged.

Do not silently pretend the requested volume was executed.

---

# 18. Monetary Risk Calculation

Before execution calculate:

```text
Loss if SL is reached
```

Prefer broker/MT5-native calculations where supported.

Conceptually:

```text
Risk = abs(
    ProfitLoss(
        symbol,
        direction,
        executed_lot,
        entry,
        stop_loss
    )
)
```

Display:

```text
Executed lot
Dollar risk
Equity risk %
Potential reward
Actual R:R
```

Example:

```text
Lot:             0.04
Risk to SL:      $31.72
Potential TP2:   $47.61
Actual RR:       1:1.50
Equity Exposure: 8.4%
```

---

# 19. Risk Display Does NOT Gate Execution

This is authoritative.

Plough Backer MUST NOT reject a valid trade merely because:

* risk percentage is high;
* dollar risk is high;
* lot progression is aggressive;
* a particular mode has reached a large volume;
* drawdown is high.

The six progression methods determine position sizing.

They do not determine signal eligibility.

High exposure should generate warnings.

Example:

```text
⚠️ HIGH EXPOSURE

Lot: 0.32
Risk to SL: $247.60
Equity Risk: 41.3%

Trade executed according to Mode 1.
```

---

# 20. Equity Lock

Equity Lock is the explicit user-defined safety gate.

Example:

```text
Current equity:   $800
Equity lock:      $500
Proposed SL loss: $350
Projected equity: $450
```

Since:

```text
$450 < $500
```

the trade MUST NOT execute.

State:

```text
BLOCKED_EQUITY_LOCK
```

Telegram:

```text
🔒 TRADE BLOCKED — EQUITY LOCK

Symbol: XAUUSD
Proposed Lot: 0.32

Current Equity:   $800.00
Risk to SL:       $350.00
Projected Equity: $450.00
Protected Equity: $500.00

Signal recorded.
Progression unchanged.
```

A blocked trade MUST NOT:

* count as a win;
* count as a loss;
* increment Mode 5's trade counter;
* alter theoretical lot;
* affect active progression state.

---

# 21. Broker Constraints

Broker/MT5 limitations are different from Plough Backer's risk rules.

Execution may legitimately fail because of:

* insufficient margin;
* invalid volume;
* volume above broker maximum;
* market closed;
* symbol unavailable;
* trading disabled;
* invalid stops;
* stale quote;
* rejected order;
* connection failure;
* broker stop-level restrictions.

These failures MUST NOT be classified as trading losses.

They MUST NOT advance progression.

---

# 22. Progression Settlement

Progression state changes only when a real executed trade reaches a deterministically classified terminal outcome.

Minimum outcome classifications:

```text
WIN
LOSS
BREAKEVEN
OTHER
```

WIN and LOSS update the selected progression according to its rule.

BREAKEVEN and OTHER MUST NOT be silently interpreted.

Default V1:

```text
BREAKEVEN → progression unchanged
OTHER     → progression unchanged + review flag
```

---

# 23. Source Isolation

Progression state should be configurable as either:

```text
GLOBAL
```

or:

```text
PER_SOURCE
```

Recommended V1 default:

```text
PER_SOURCE
```

This prevents a winning streak from Provider A unexpectedly increasing the lot applied to Provider B.

The journal must always identify the source.

---

# 24. Duplicate Protection

Every normalized signal requires a deterministic fingerprint.

Candidate fields:

```text
source_id
message_id
symbol
direction
entry
SL
selected_TP
```

Generate:

```text
SHA-256 fingerprint
```

Before execution:

```text
if fingerprint already executed:
    REJECTED_DUPLICATE
```

Duplicate protection MUST survive application restarts.

---

# 25. Telegram Message Edits

V1 policy:

> Once an order has been successfully executed, subsequent edits to the original Telegram message MUST NOT automatically modify the MT5 position.

Instead:

```text
⚠️ SOURCE MESSAGE EDITED

Signal: PB-000184
MT5 Ticket: 123456

The source message changed after execution.
V1 does not automatically modify active positions.
```

Store both original and edited content.

Future versions may add controlled SL/TP amendment support.

---

# 26. Concurrency

Signals may arrive almost simultaneously.

Progression calculation MUST be serialized per progression scope.

Two concurrent signals MUST NOT both read:

```text
current lot = 0.04
```

when one should have advanced the state.

Use transaction-level or application-level locking.

A progression state update and trade settlement must be atomic.

---

# 27. Idempotency

All critical actions require idempotency.

Especially:

```text
Telegram ingestion
Signal creation
Execution request
MT5 ticket association
Position closure
Progression update
Scheduled report generation
```

Restarting the application must never cause an already-executed signal to execute again.

---

# 28. MT5 Execution

Before sending an order:

1. verify MT5 connection;
2. resolve symbol;
3. ensure symbol is visible/selectable;
4. retrieve symbol specification;
5. obtain current tick;
6. determine order type;
7. calculate theoretical lot;
8. normalize executable lot;
9. calculate SL risk;
10. calculate TP reward;
11. calculate actual R:R;
12. evaluate Equity Lock;
13. check duplicate/idempotency state;
14. construct MT5 request;
15. submit;
16. validate retcode;
17. persist response;
18. notify Telegram.

Never mark a trade EXECUTED solely because `order_send()` returned an object.

Validate MT5's result code.

---

# 29. Execution Reconciliation

Plough Backer must not assume its local state is correct.

Periodically reconcile:

```text
Local OPEN trades
       ↕
MT5 positions/orders/history
```

Detect:

* externally closed trades;
* TP closures;
* SL closures;
* manually closed trades;
* cancelled pending orders;
* missing positions;
* application downtime closures.

MT5 history is the authoritative source for realized trading results.

---

# 30. Restart Recovery

After application restart:

1. connect to SQLite;
2. validate migrations;
3. connect Telegram;
4. connect MT5;
5. load active configuration;
6. load progression state;
7. load unresolved executions;
8. reconcile open positions;
9. reconcile MT5 history;
10. process settlements idempotently;
11. resume Telegram monitoring.

Restart MUST NOT reset lot progression.

---

# 31. Telegram Control Interface

Main dashboard:

```text
🚜 PLOUGH BACKER

Status:       🟢 Running
Mode:         DEMO
MT5:          🟢 Connected
Telegram:     🟢 Connected

Balance:      $347.21
Equity:       $341.80

Risk Method:
1️⃣ Anti-Martingale

Current Theoretical Lot: 0.0400
Next Executable Lot:     0.04

Today:
Trades: 3
Wins:   3
Losses: 0
P/L:    +$54.30

[📊 Performance]
[⚙️ Risk Mode]

[📡 Sources]
[📋 Trades]

[🔒 Equity Lock]
[🧪 Shadow]

[⏸ Pause]
[⛔ Stop]
```

Keep the UI compact.

---

# 32. Risk Method Selector

Telegram:

```text
⚙️ SELECT PLOUGH-BACK METHOD

1️⃣ Anti-Martingale
2️⃣ Martingale
3️⃣ Win ×2 / Loss ÷2
4️⃣ Always ×2
5️⃣ ×2 Every 5 Trades
6️⃣ Win +50% / Loss Hold

Current:
1️⃣ Anti-Martingale
```

Changing mode requires confirmation.

Example:

```text
Change from:

1️⃣ Anti-Martingale

to:

5️⃣ ×2 Every 5 Trades?

[✅ Confirm]
[❌ Cancel]
```

Persist configuration immediately.

---

# 33. Mode Change Policy

By default, switching risk methods should reset the newly selected method to the symbol/source base state.

Do not transfer an incompatible progression automatically.

Record:

```text
old_mode
new_mode
old_theoretical_lot
new_theoretical_lot
timestamp
initiating_user
```

Future versions may support explicit state migration.

---

# 34. Pause

PAUSE means:

```text
Telegram listening: YES
Parsing:            YES
Journaling:         YES
Shadow Mode:        YES
MT5 new execution:  NO
Existing positions: UNCHANGED
```

Signals received while paused should be recorded as:

```text
SKIPPED_PAUSED
```

They do not alter active progression.

---

# 35. Kill Switch

STOP means:

```text
No new trades.
```

It MUST NOT automatically close existing MT5 positions.

Closing all positions is a materially different action and must not be bundled into the kill switch.

---

# 36. Shadow Mode

Shadow Mode is a first-class V1 feature.

Every valid signal should be replayed against all six progression methods independently.

Only one method controls the real/demo MT5 account.

The other five are virtual.

Example:

```text
ACTIVE:
Mode 1

SHADOW:
Mode 2
Mode 3
Mode 4
Mode 5
Mode 6
```

Each shadow mode maintains independent:

```text
virtual balance
virtual equity
theoretical lot
normalized lot
wins
losses
trade count
peak equity
drawdown
ruin/failure state
```

---

# 37. Shadow Execution

Shadow calculations MUST use:

* actual signal entry;
* actual SL;
* selected TP;
* broker symbol specification;
* broker minimum lot;
* broker lot step;
* actual chronological signal sequence.

Do NOT estimate shadow performance from overall win rate.

The chronological sequence matters.

---

# 38. Shadow Failure

If a shadow mode reaches a position that could not have been executed because its virtual account lacks sufficient capital/margin, record:

```text
SHADOW_FAILED
```

Do not pretend it continued normally.

Report:

```text
3️⃣ Win ×2 / Loss ÷2
❌ Failed at Trade 19
Reason: Insufficient simulated margin
```

---

# 39. Equity Lock Controls

Telegram:

```text
🔒 EQUITY LOCK

Status: ENABLED
Protected Equity: $500.00

[Change Amount]
[Disable]
```

Enabling/changing the lock requires confirmation.

Every change must be journaled.

---

# 40. Trade Notification

After successful execution:

```text
🚜 TRADE EXECUTED

XAUUSD BUY

Source: Gold Signals
Entry: 4500.20
SL:    4492.20
TP2:   4512.20

Method:
1️⃣ Anti-Martingale

Theoretical Lot: 0.0400
Executed Lot:    0.04

Risk to SL:      $32.00
TP2 Reward:      $48.00
Actual RR:       1:1.50
Equity Risk:     6.7%

MT5 Ticket: #123456
Signal: PB-000184
```

---

# 41. Closure Notification

Example:

```text
✅ TRADE CLOSED — WIN

XAUUSD BUY
Ticket: #123456

P/L:       +$47.62
Result:    WIN
R Multiple:+1.49R

Mode:
1️⃣ Anti-Martingale

Previous theoretical lot: 0.0400
Next theoretical lot:     0.0800

Balance: $812.42
```

Loss:

```text
❌ TRADE CLOSED — LOSS

P/L: -$31.84

Mode:
1️⃣ Anti-Martingale

Previous theoretical lot: 0.0400
Next theoretical lot:     0.0100
```

---

# 42. Journal

Every trade must store enough information to reconstruct what happened.

Minimum fields:

```text
id
signal_id
signal_fingerprint

telegram_source_id
telegram_message_id
telegram_timestamp
raw_signal

parser_version

symbol_raw
symbol_mt5
direction
order_type

requested_entry
actual_entry
stop_loss

tp1
tp2
tp3
selected_tp

risk_mode

base_lot
theoretical_lot
executed_lot

volume_min
volume_max
volume_step

estimated_risk
estimated_reward
estimated_rr
equity_risk_percent

balance_before
equity_before
margin_before
free_margin_before

equity_lock_enabled
equity_lock_value

mt5_order_id
mt5_deal_id
mt5_position_id

execution_retcode
execution_message

opened_at
closed_at

close_price
realized_profit
realized_swap
realized_commission
net_profit

result
realized_r_multiple

balance_after
equity_after

created_at
updated_at
```

---

# 43. Audit Events

Important system actions require immutable-style audit records.

Examples:

```text
APP_STARTED
APP_STOPPED
MT5_CONNECTED
MT5_DISCONNECTED
TELEGRAM_CONNECTED
SIGNAL_RECEIVED
SIGNAL_PARSED
SIGNAL_REJECTED
SIGNAL_DUPLICATE
TRADE_REQUESTED
TRADE_EXECUTED
TRADE_REJECTED
TRADE_SETTLED
MODE_CHANGED
EQUITY_LOCK_CHANGED
PAUSED
RESUMED
KILL_SWITCH
REPORT_GENERATED
```

Audit records should contain structured metadata.

---

# 44. Analytics

At minimum calculate:

```text
Starting balance
Ending balance
Net P/L
Return %
Total trades
Wins
Losses
Breakeven
Win rate

Gross profit
Gross loss
Profit factor

Average win
Average loss

Average realized R
Expectancy in R

Largest win
Largest loss

Longest win streak
Longest loss streak

Peak equity
Maximum drawdown $
Maximum drawdown %

Average lot
Maximum lot

Average monetary risk
Maximum monetary risk

Average equity exposure
Maximum equity exposure
```

---

# 45. Source Analytics

Track performance independently for each Telegram source.

Example:

```text
SOURCE: Gold Signals

Signals Received: 67
Valid Signals:    64
Executed:         61

Wins:   50
Losses: 11

Win Rate: 81.97%

Net P/L: +$...
Average R: ...
Profit Factor: ...

Gold Performance: ...
Synthetic Performance: ...
```

---

# 46. Instrument Analytics

Break performance down by symbol.

Example:

```text
XAUUSD
Trades: 31
Wins: 26
Losses: 5
Win Rate: 83.9%
Net: +$...

Boom 1000
Trades: 11
Wins: 8
Losses: 3
...
```

This prevents overall win rate from hiding weak instruments.

---

# 47. Weekly Report

Generate automatically on configured schedule.

Example:

```text
🚜 PLOUGH BACKER
WEEKLY REPORT

Period:
14 Sep – 20 Sep 2026

ACCOUNT

Opening Balance: $100.00
Closing Balance: $168.40

Net P/L: +$68.40
Return:  +68.40%

TRADES

Total: 12
Wins:  10
Losses: 2

Win Rate: 83.33%

Gross Profit: ...
Gross Loss:   ...
Profit Factor:...

Average R: ...
Expectancy:...

RISK

Method:
1️⃣ Anti-Martingale

Base Lot:        0.01
Highest Lot:     0.04
Average Lot:     0.018

Highest $ Risk:  $32.10
Highest Equity Exposure: 19.1%

DRAWDOWN

Max DD:       $...
Max DD:       ...%
Longest W:    6
Longest L:    1

INSTRUMENTS

Gold:
8 trades | 7W / 1L

Synthetics:
4 trades | 3W / 1L
```

---

# 48. Weekly Shadow Comparison

Append:

```text
🧪 SHADOW LAB

Same signals replayed through all methods:

1️⃣ Anti-Martingale
Virtual Balance: $168.40
Return: +68.4%
Max DD: ...

2️⃣ Martingale
Virtual Balance: $144.20
Return: +44.2%
Max DD: ...

3️⃣ Win×2 / Loss÷2
Virtual Balance: $191.30
Return: +91.3%
Max DD: ...

4️⃣ Always×2
❌ Failed on Trade 9

5️⃣ ×2 Every 5
Virtual Balance: $156.70

6️⃣ Win+50% / Loss Hold
Virtual Balance: $173.80
```

No recommendation is required.

Report the deterministic results.

---

# 49. Monthly Report

Monthly reports include everything from weekly reporting plus:

* balance growth;
* cumulative P/L;
* total R;
* performance by week;
* performance by source;
* performance by instrument;
* progression-method performance;
* Shadow Mode comparison;
* largest drawdown;
* largest lot reached;
* largest dollar exposure;
* average execution latency;
* broker rejection count;
* parser rejection count;
* duplicate count;
* Equity Lock block count;
* MT5 connectivity incidents.

---

# 50. Execution Latency

Store timestamps:

```text
telegram_received_at
parsed_at
execution_requested_at
broker_response_at
position_confirmed_at
```

Calculate:

```text
Telegram → Parser
Parser → MT5 request
MT5 request → broker response
End-to-end latency
```

This is particularly useful for signal copying.

---

# 51. Database

Recommended SQLite tables:

```text
settings
signal_sources
signals
executions
positions
trade_results
progression_states
shadow_states
shadow_trades
equity_locks
audit_events
reports
symbol_mappings
```

Use migrations.

Do not initialize production/demo state using ad-hoc `CREATE TABLE` statements during normal startup.

---

# 52. Progression State Model

Suggested:

```text
scope_id
symbol_or_scope
mode

base_lot
theoretical_lot

completed_trades
wins
losses

mode5_block_trade_count

version
updated_at
```

Use optimistic or transactional locking.

---

# 53. Configuration

Secrets belong in environment variables.

Example:

```env
APP_ENV=demo

EXECUTION_MODE=DEMO

TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_BOT_TOKEN=

TELEGRAM_ADMIN_USER_ID=

MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=

DATABASE_URL=sqlite:///data/plough_backer.db

DEFAULT_RISK_MODE=1

EQUITY_LOCK_ENABLED=false
EQUITY_LOCK_VALUE=

REPORT_TIMEZONE=UTC
```

Never commit credentials.

Provide:

```text
.env.example
```

---

# 54. Security

At minimum:

* secrets excluded from Git;
* `.env` ignored;
* Telegram bot commands restricted to authorized admin IDs;
* database directory access restricted;
* logs sanitized;
* passwords never logged;
* Telegram tokens never logged;
* MT5 credentials never logged;
* raw broker requests sanitized where necessary;
* configuration changes audited.

Unauthorized Telegram users must not be able to:

* change mode;
* pause trading;
* resume;
* activate kill switch;
* alter Equity Lock;
* view sensitive account information.

---

# 55. Observability

Use structured logs.

Example:

```json
{
  "event": "trade_executed",
  "signal_id": "PB-000184",
  "symbol": "XAUUSD",
  "mode": 1,
  "theoretical_lot": "0.04",
  "executed_lot": "0.04",
  "mt5_ticket": 123456
}
```

Log levels:

```text
DEBUG
INFO
WARNING
ERROR
CRITICAL
```

Do not rely solely on Telegram messages for operational evidence.

---

# 56. Health Checks

The application should continuously know:

```text
Telegram connected?
MT5 connected?
Database writable?
Scheduler running?
Listener alive?
Last signal received?
Last MT5 reconciliation?
```

Telegram status:

```text
🩺 SYSTEM STATUS

Telegram: 🟢
MT5:      🟢
Database: 🟢
Scheduler:🟢

Mode: DEMO
Trading: RUNNING

Last Reconciliation:
10:42:16 UTC
```

---

# 57. MT5 Disconnect Behaviour

If MT5 disconnects:

```text
DO NOT execute.
```

Continue receiving and recording Telegram signals.

Mark:

```text
SKIPPED_MT5_UNAVAILABLE
```

Send notification.

Reconnect using bounded retry/backoff.

Do not execute old skipped signals automatically after reconnection unless explicitly designed in a future version.

---

# 58. Telegram Disconnect Behaviour

Reconnect using bounded retry/backoff.

Record downtime.

On reconnect, safely retrieve missed messages where Telegram API semantics allow.

Deduplication MUST prevent replay execution.

---

# 59. Testing Strategy

V1 requires automated tests.

Minimum categories:

```text
unit/
integration/
replay/
mt5_demo/
```

---

# 60. Progression Unit Tests

Every mode requires deterministic sequence tests.

Example Mode 1:

```text
Input:
W W L W

Expected theoretical lots:
0.01
0.02
0.04
0.01
```

Mode 2:

```text
L L W L

0.01
0.02
0.04
0.01
```

Mode 3:

```text
W W L L

0.01
0.02
0.04
0.02
```

Mode 4:

```text
W L W L

0.01
0.02
0.04
0.08
```

Mode 5:

```text
Trades 1–5  = 0.01
Trades 6–10 = 0.02
```

Mode 6:

```text
W W L W

0.010000
0.015000
0.022500
0.022500
```

and next:

```text
0.033750
```

---

# 61. Lot Normalization Tests

Test:

```text
minimum = 0.01
step    = 0.01
```

against:

```text
0.010000
0.015000
0.022500
0.033750
0.050625
```

Verify both:

```text
theoretical_lot
executed_lot
```

The theoretical value MUST remain unchanged by normalization.

---

# 62. Parser Tests

Create fixtures for every supported source format.

Test:

* BUY;
* SELL;
* Gold;
* synthetic;
* multiple TP;
* TP2;
* missing SL;
* missing TP2;
* malformed number;
* duplicate;
* edited message;
* unsupported symbol;
* pending order.

Every fixture must have an explicit expected result.

---

# 63. Equity Lock Tests

Example:

```text
Equity = $800
Lock   = $500
Risk   = $200

Projected = $600
```

Expected:

```text
EXECUTE
```

Then:

```text
Risk = $350
Projected = $450
```

Expected:

```text
BLOCKED_EQUITY_LOCK
```

Verify progression remains unchanged.

---

# 64. Idempotency Tests

Simulate:

1. signal received;
2. trade executed;
3. process crashes;
4. application restarts;
5. Telegram message encountered again.

Expected:

```text
ONE MT5 EXECUTION ONLY
```

---

# 65. Settlement Idempotency

Simulate MT5 returning the same closed deal repeatedly during reconciliation.

Progression MUST update exactly once.

---

# 66. Concurrency Tests

Send two valid signals simultaneously.

Verify progression state is serialized correctly and no state update is lost.

---

# 67. Replay Testing

Build a replay runner.

Input:

```text
historical_signals.json
```

It should deterministically replay historical chronological signals through:

```text
Mode 1
Mode 2
Mode 3
Mode 4
Mode 5
Mode 6
```

Output:

```text
final virtual balance
wins
losses
maximum lot
maximum exposure
maximum drawdown
failure point
```

This becomes the core validation mechanism for Shadow Mode.

---

# 68. Demo Acceptance Test

V1 is not complete until it passes an actual MT5 demo rehearsal.

Required scenarios:

1. Gold BUY;
2. Gold SELL;
3. Gold signal with TP1/TP2/TP3;
4. verify TP2 selected;
5. synthetic BUY;
6. synthetic SELL;
7. Mode 1 progression;
8. Mode 2 progression;
9. Mode 3 progression;
10. Mode 4 progression;
11. Mode 5 progression;
12. Mode 6 theoretical accumulation;
13. broker volume normalization;
14. SL closure;
15. TP closure;
16. manual MT5 closure;
17. duplicate Telegram message;
18. malformed signal;
19. Equity Lock rejection;
20. pause;
21. resume;
22. kill switch;
23. MT5 disconnect;
24. application restart;
25. post-restart reconciliation;
26. weekly report;
27. monthly report;
28. Shadow Mode reconciliation.

All must have evidence.

---

# 69. Definition of Done

V1 is **DEMO_READY** only when:

```text
[ ] Telegram source successfully monitored
[ ] Authorized bot controls working
[ ] Deterministic parsing working
[ ] Gold symbol resolution working
[ ] Synthetic resolution working
[ ] TP2 Gold rule verified
[ ] MT5 demo execution verified
[ ] BUY verified
[ ] SELL verified
[ ] SL verified
[ ] TP verified

[ ] Mode 1 verified
[ ] Mode 2 verified
[ ] Mode 3 verified
[ ] Mode 4 verified
[ ] Mode 5 verified
[ ] Mode 6 verified

[ ] Accumulated theoretical sizing verified
[ ] Broker volume normalization verified
[ ] Monetary risk display verified
[ ] Equity exposure display verified

[ ] Equity Lock verified
[ ] Duplicate protection verified
[ ] Idempotency verified
[ ] Restart recovery verified
[ ] Position reconciliation verified

[ ] Pause verified
[ ] Resume verified
[ ] Kill switch verified

[ ] Shadow Mode verified
[ ] Weekly report verified
[ ] Monthly report verified

[ ] Unit tests passing
[ ] Integration tests passing
[ ] Replay tests passing
[ ] Demo acceptance test passing

[ ] No credentials committed
[ ] Structured logging enabled
[ ] Database migrations working
[ ] README deployment instructions verified
```

---

# 70. Recommended Repository Structure

```text
plough-backer/
│
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── alembic.ini
│
├── config/
│   ├── symbols.yaml
│   └── sources.yaml
│
├── src/
│   └── plough_backer/
│       │
│       ├── main.py
│       │
│       ├── config.py
│       │
│       ├── enums.py
│       │
│       ├── exceptions.py
│       │
│       ├── logging.py
│       │
│       ├── decimal_utils.py
│       │
│       ├── telegram/
│       │   ├── listener.py
│       │   ├── bot.py
│       │   ├── handlers.py
│       │   └── notifications.py
│       │
│       ├── signals/
│       │   ├── models.py
│       │   ├── parser.py
│       │   ├── validators.py
│       │   ├── fingerprint.py
│       │   └── symbol_resolver.py
│       │
│       ├── risk/
│       │   ├── engine.py
│       │   ├── normalization.py
│       │   ├── equity_lock.py
│       │   └── modes/
│       │       ├── base.py
│       │       ├── anti_martingale.py
│       │       ├── martingale.py
│       │       ├── win_double_loss_half.py
│       │       ├── always_double.py
│       │       ├── five_trade_step.py
│       │       └── win_half_increment.py
│       │
│       ├── trading/
│       │   ├── mt5_client.py
│       │   ├── executor.py
│       │   ├── calculator.py
│       │   ├── reconciliation.py
│       │   └── settlement.py
│       │
│       ├── shadow/
│       │   ├── engine.py
│       │   └── portfolio.py
│       │
│       ├── analytics/
│       │   ├── metrics.py
│       │   ├── weekly.py
│       │   ├── monthly.py
│       │   └── source_analysis.py
│       │
│       ├── persistence/
│       │   ├── database.py
│       │   ├── models.py
│       │   └── repositories.py
│       │
│       └── scheduler/
│           └── jobs.py
│
├── migrations/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── replay/
│   ├── fixtures/
│   └── mt5_demo/
│
├── scripts/
│   ├── health_check.py
│   ├── replay.py
│   └── demo_acceptance.py
│
└── data/
    └── .gitkeep
```

---

# 71. Implementation Order

A coding agent SHOULD implement V1 in this order.

## Phase 1 — Domain

Implement:

* enums;
* normalized signal;
* trade model;
* progression state;
* Decimal handling;
* all six progression methods.

Write tests first.

No Telegram.

No MT5.

---

## Phase 2 — Lot Engine

Implement:

* theoretical sizing;
* broker-step normalization;
* minimum/maximum handling;
* risk calculations;
* Equity Lock.

Complete unit tests.

---

## Phase 3 — Persistence

Implement:

* SQLite;
* SQLAlchemy;
* Alembic;
* signals;
* trades;
* progression;
* audit events;
* shadow states.

Test restart persistence.

---

## Phase 4 — Signal Parsing

Implement deterministic source profiles.

Create fixture-driven tests.

Malformed messages MUST fail closed.

---

## Phase 5 — MT5 Adapter

Implement:

* connection;
* account information;
* symbol specification;
* tick retrieval;
* order validation;
* order submission;
* history;
* reconciliation.

Do not mix progression logic into the MT5 adapter.

---

## Phase 6 — Execution Orchestrator

Connect:

```text
Normalized Signal
→ progression
→ volume normalization
→ monetary risk
→ Equity Lock
→ MT5
→ persistence
```

Add idempotency.

---

## Phase 7 — Telegram

Add:

* listener;
* admin bot;
* dashboard;
* mode selection;
* Equity Lock;
* pause;
* resume;
* stop;
* notifications.

---

## Phase 8 — Settlement

Reconcile closed MT5 positions and update progression exactly once.

This phase is critical.

---

## Phase 9 — Shadow Mode

Replay every valid signal independently through all six virtual portfolios.

---

## Phase 10 — Analytics

Implement:

* metrics;
* weekly report;
* monthly report;
* source analysis;
* instrument analysis.

---

## Phase 11 — Hardening

Add:

* restart recovery;
* concurrency protection;
* reconnect logic;
* structured logging;
* health checks;
* audit events;
* failure tests.

---

## Phase 12 — Demo Gate

Run complete MT5 demo acceptance suite.

Do not declare V1 complete before this gate.

---

# 72. Critical Invariants

These MUST always hold.

### INV-01

A Telegram message can never create more than one real MT5 execution unless explicitly designed as a multi-entry signal.

### INV-02

A progression changes only because of a SETTLED executed trade.

### INV-03

An Equity-Lock-blocked trade does not alter progression.

### INV-04

A broker-rejected trade does not alter progression.

### INV-05

A paused/skipped signal does not alter progression.

### INV-06

Lot normalization never modifies stored theoretical progression.

### INV-07

Theoretical calculations use Decimal.

### INV-08

Gold uses TP2 according to configured Gold policy.

### INV-09

Monetary risk is calculated from actual symbol specifications and SL distance, not assumed from lot size.

### INV-10

High calculated risk alone does not block execution.

### INV-11

Equity Lock may block execution.

### INV-12

MT5/broker impossibility may prevent execution.

### INV-13

Shadow methods never affect the real account.

### INV-14

MT5 trade history is authoritative for realized P/L.

### INV-15

Restarting Plough Backer cannot duplicate an already-executed signal.

---

# 73. Deployment Model

For V1, deploy on the machine running MetaTrader 5.

Recommended:

```text
Windows
│
├── MetaTrader 5 Terminal
│
└── Plough Backer Python Process
```

This avoids unnecessary remote MT5 complexity.

The service should support automatic startup.

Recommended production-like demo deployment:

```text
Windows Task Scheduler
```

or an appropriate Windows service wrapper.

Configure automatic restart after failure.

---

# 74. Startup Command

Development:

```bash
python -m plough_backer.main
```

Example health check:

```bash
python scripts/health_check.py
```

Replay:

```bash
python scripts/replay.py --input tests/fixtures/historical_signals.json
```

Demo acceptance:

```bash
python scripts/demo_acceptance.py
```

---

# 75. Safe Demo Rollout

Use progressive activation.

### Stage A

```text
PAPER
```

Telegram connected.

No MT5 execution.

Validate parser.

---

### Stage B

```text
DEMO + one source + Mode 1
```

Validate real MT5 demo orders.

---

### Stage C

Enable Shadow Mode.

Compare all six methods.

---

### Stage D

Add synthetic symbols.

---

### Stage E

Enable weekly/monthly reporting.

---

### Stage F

Run sustained demo test.

Recommended:

```text
≥ 30 valid chronological signals
```

before considering V1 demo validation complete.

---

# 76. README Rule for Coding Agents

When implementing this repository:

> **Do not infer new trading rules when this specification is silent.**

If implementation requires a decision that changes:

* signal interpretation;
* position sizing;
* progression;
* TP selection;
* trade eligibility;
* settlement;
* Equity Lock;
* Shadow Mode;

stop and document the ambiguity.

Infrastructure implementation details may be chosen normally.

Trading semantics may not.

---

# 77. Product Philosophy

Plough Backer is not intended to predict markets.

It is not intended to determine whether a signal provider is correct.

It is not intended to use AI to manufacture trading decisions.

Its job is simpler:

> **Receive → Parse → Size → Protect → Execute → Reconcile → Record → Analyze.**

The signal provider supplies the trading idea.

Plough Backer supplies deterministic execution and plough-back mechanics.

---

# 78. Final V1 Contract

A valid signal enters Plough Backer.

The system identifies the source and instrument.

For Gold, TP2 is selected.

The connected broker provides the minimum executable lot and symbol specifications.

The active Plough-Back mode calculates the next **theoretical lot**.

The theoretical lot is converted to the nearest broker-valid **executable lot** without destroying the theoretical progression.

Plough Backer calculates and displays:

```text
lot
dollar risk
equity exposure
potential reward
actual R:R
```

The size of that risk does not itself prevent execution.

If Equity Lock would be violated, execution is blocked.

If the broker cannot execute the order, the failure is recorded.

Otherwise, the trade is submitted to MT5.

The resulting position is reconciled against MT5 until closure.

Its actual result updates the active progression exactly once.

Every other progression method processes the same chronological signal in Shadow Mode without affecting the real/demo account.

Every material action is persisted.

Weekly and monthly reports describe what actually happened.

After a restart, the system resumes from persisted truth rather than resetting its progression.

That is **Plough Backer V1**.

---

# 79. V1 Acceptance Statement

The project may be labelled:

```text
PLOUGH BACKER V1 — DEMO_READY
```

only after:

```text
Automated tests: PASS
Replay tests:    PASS
Restart tests:   PASS
Idempotency:     PASS
Shadow Mode:     PASS
MT5 Demo Gate:   PASS
Credentials:     CLEAN
```

`DEMO_READY` does **not** mean live-production authorization.

Live deployment should be treated as a separate acceptance decision after sufficient demo evidence has been collected.

---

**End of V1 specification.**
