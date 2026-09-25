"""Demo acceptance evidence (README §68, §69, §75 Stage F): python scripts/demo_acceptance.py

Reads the journal of a real MT5 DEMO run and reports, per §68 scenario, the evidence found:
PASS (evidence in the journal), MISSING (not exercised yet) or MANUAL (needs a human check).
It never marks a scenario PASS without a journal record behind it. Writes a Markdown
evidence file with the matching signal ids / audit ids for each scenario.

Run it on the Windows MT5 machine against the demo database, after exercising the
scenarios. It does not place trades itself.
"""

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from plough_backer.config import DatabaseSettings, SymbolsConfig, load_symbols
from plough_backer.enums import AssetClass, AuditEventType, SignalState
from plough_backer.persistence.database import make_engine, session_factory
from plough_backer.persistence.models import AuditEvent, Signal, Trade

MIN_SUSTAINED_SIGNALS = 30  # §75 Stage F


@dataclass(frozen=True)
class Result:
    number: int
    scenario: str
    status: str  # PASS | MISSING | MANUAL
    evidence: str


def _trades(s: Session, *where: object) -> list[Trade]:
    return list(s.scalars(select(Trade).where(Trade.mt5_order_id.is_not(None), *where)).all())


def _states(s: Session, state: SignalState) -> list[str]:
    return list(s.scalars(select(Signal.signal_id).where(Signal.state == state)).all())


def _audits(s: Session, kind: AuditEventType) -> list[AuditEvent]:
    return list(s.scalars(select(AuditEvent).where(AuditEvent.event_type == kind)).all())


def collect(s: Session, symbols: SymbolsConfig) -> list[Result]:
    gold = {m.mt5_symbol for m in symbols.symbols.values() if m.asset_class is AssetClass.GOLD}
    synth = {
        m.mt5_symbol for m in symbols.symbols.values() if m.asset_class is AssetClass.SYNTHETIC
    }
    settled = list(s.scalars(select(Trade).where(Trade.settled_at.is_not(None))).all())
    started = sorted(e.at for e in _audits(s, AuditEventType.APP_STARTED))
    reports = [e.payload.get("report", "") for e in _audits(s, AuditEventType.REPORT_GENERATED)]

    def ids(rows: list[Trade]) -> str:
        return ", ".join(t.signal_id for t in rows[:5])

    def mode_settled(mode: int) -> list[Trade]:
        return [t for t in settled if t.risk_mode == mode and t.result in ("WIN", "LOSS")]

    checks: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
        (
            "Gold BUY",
            lambda: _ok(_trades(s, Trade.symbol_mt5.in_(gold), Trade.direction == "BUY"), ids),
        ),
        (
            "Gold SELL",
            lambda: _ok(_trades(s, Trade.symbol_mt5.in_(gold), Trade.direction == "SELL"), ids),
        ),
        (
            "Gold signal with TP1/TP2/TP3",
            lambda: _ok(
                _trades(
                    s,
                    Trade.symbol_mt5.in_(gold),
                    Trade.tp1.is_not(None),
                    Trade.tp2.is_not(None),
                    Trade.tp3.is_not(None),
                ),
                ids,
            ),
        ),
        (
            "TP2 selected for Gold",
            lambda: _ok(
                [t for t in _trades(s, Trade.symbol_mt5.in_(gold)) if t.selected_tp == t.tp2], ids
            ),
        ),
        (
            "Synthetic BUY",
            lambda: _ok(_trades(s, Trade.symbol_mt5.in_(synth), Trade.direction == "BUY"), ids),
        ),
        (
            "Synthetic SELL",
            lambda: _ok(_trades(s, Trade.symbol_mt5.in_(synth), Trade.direction == "SELL"), ids),
        ),
        *[(f"Mode {m} progression", (lambda m=m: _ok(mode_settled(m), ids))) for m in range(1, 7)],
        (
            "Broker volume normalization",
            lambda: _ok([t for t in _trades(s) if t.theoretical_lot != t.executed_lot], ids),
        ),
        (
            "SL closure",
            lambda: _ok(
                [t for t in settled if t.result == "LOSS" and t.close_price == t.stop_loss], ids
            ),
        ),
        (
            "TP closure",
            lambda: _ok(
                [t for t in settled if t.result == "WIN" and t.close_price == t.selected_tp], ids
            ),
        ),
        ("Manual MT5 closure", lambda: _ok([t for t in settled if t.result == "OTHER"], ids)),
        (
            "Duplicate Telegram message",
            lambda: _list([str(e.id) for e in _audits(s, AuditEventType.SIGNAL_DUPLICATE)]),
        ),
        (
            "Malformed signal",
            lambda: _list(
                _states(s, SignalState.REJECTED_PARSE)
                + _states(s, SignalState.REJECTED_INVALID_SIGNAL)
            ),
        ),
        ("Equity Lock rejection", lambda: _list(_states(s, SignalState.BLOCKED_EQUITY_LOCK))),
        ("Pause", lambda: _list(_states(s, SignalState.SKIPPED_PAUSED))),
        ("Resume", lambda: _list([str(e.id) for e in _audits(s, AuditEventType.RESUMED)])),
        ("Kill switch", lambda: _list(_states(s, SignalState.SKIPPED_STOPPED))),
        (
            "MT5 disconnect",
            lambda: _list(
                [str(e.id) for e in _audits(s, AuditEventType.MT5_DISCONNECTED)]
                + _states(s, SignalState.SKIPPED_MT5_UNAVAILABLE)
            ),
        ),
        ("Application restart", lambda: (len(started) >= 2, f"{len(started)} APP_STARTED")),
        (
            "Post-restart reconciliation",
            lambda: _ok(
                [
                    t
                    for t in settled
                    if t.opened_at
                    and t.settled_at
                    and any(t.opened_at < a <= t.settled_at for a in started[1:])
                ],
                ids,
            ),
        ),
        ("Weekly report", lambda: _list([r for r in reports if r.startswith("weekly")])),
        ("Monthly report", lambda: _list([r for r in reports if r.startswith("monthly")])),
    ]
    results = [
        Result(i, name, "PASS" if ok else "MISSING", ev)
        for i, (name, check) in enumerate(checks, start=1)
        for ok, ev in [check()]
    ]
    results.append(
        Result(
            28,
            "Shadow Mode reconciliation",
            "MANUAL",
            "Compare the report's SHADOW LAB active-method line with real P/L.",
        )
    )
    return results


def _ok(rows: list[Trade], ids: Callable[[list[Trade]], str]) -> tuple[bool, str]:
    return bool(rows), ids(rows) if rows else "no journal record"


def _list(items: list[str]) -> tuple[bool, str]:
    return bool(items), ", ".join(items[:5]) if items else "no journal record"


def sustained_signals(s: Session) -> int:
    """§75 Stage F: valid signals = parsed and not rejected as invalid/parse/duplicate."""
    rejected = (
        SignalState.REJECTED_PARSE,
        SignalState.REJECTED_INVALID_SIGNAL,
        SignalState.REJECTED_DUPLICATE,
    )
    return (
        s.scalar(
            select(func.count())
            .select_from(Signal)
            .where(Signal.parsed_at.is_not(None), Signal.state.not_in(rejected))
        )
        or 0
    )


def render(results: list[Result], valid_signals: int) -> str:
    passed = sum(r.status == "PASS" for r in results)
    lines = [
        "# Plough Backer — Demo Acceptance Evidence",
        "",
        f"Generated: {datetime.now(UTC):%Y-%m-%d %H:%M UTC}",
        "",
        f"Scenarios with evidence: **{passed} / {len(results)}** (§68)",
        "",
        f"Valid chronological signals: **{valid_signals} / {MIN_SUSTAINED_SIGNALS}** (§75 F)",
        "",
        "| # | Scenario | Status | Evidence |",
        "|---|---|---|---|",
        *[f"| {r.number} | {r.scenario} | {r.status} | {r.evidence} |" for r in results],
        "",
        "DEMO_READY requires every scenario PASS (28 confirmed manually), the §69 checklist",
        "and the §79 statement. This file is evidence, not the decision.",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("data/demo_acceptance.md"))
    args = parser.parse_args()
    engine = make_engine(DatabaseSettings().database_url)
    with session_factory(engine).begin() as s:
        results = collect(s, load_symbols())
        valid = sustained_signals(s)
    engine.dispose()
    report = render(results, valid)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(report)
    complete = all(r.status != "MISSING" for r in results) and valid >= MIN_SUSTAINED_SIGNALS
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
