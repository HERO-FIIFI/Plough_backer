"""Phase 11 hardening + Phase 12 evidence harness, against fakes (no Telegram/MT5)."""

import asyncio
import importlib.util
import logging
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from plough_backer.config import load_settings
from plough_backer.enums import SignalState
from plough_backer.exceptions import MT5Unavailable
from plough_backer.main import EXIT_CONFIG_ERROR, main
from plough_backer.persistence.database import session_factory
from plough_backer.persistence.models import AuditEvent, Trade
from plough_backer.resilience import MT5Monitor, backoff_delays, retry
from plough_backer.runtime import Runtime
from plough_backer.trading.settlement import SettlementPolicy, Settler
from tests.conftest import paper_settings
from tests.fakes import FakeGateway
from tests.integration.test_orchestrator import GOLD_MSG, SOURCES, SYMBOLS, orchestrator, send
from tests.integration.test_settlement import open_and_close

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)  # Thursday


# --- backoff (§57/§58: bounded) -----------------------------------------------------------


def test_backoff_is_exponential_and_capped() -> None:
    assert backoff_delays(6, 1, 10) == [1, 2, 4, 8, 10]


def test_retry_recovers_then_is_bounded() -> None:
    calls, slept = [0], []

    def flaky() -> str:
        calls[0] += 1
        if calls[0] < 3:
            raise MT5Unavailable("down")
        return "ok"

    assert (
        retry(flaky, attempts=5, base=1, cap=5, retry_on=(MT5Unavailable,), sleep=slept.append)
        == "ok"
    )
    assert slept == [1, 2]

    def always() -> None:
        raise MT5Unavailable("down")

    with pytest.raises(MT5Unavailable):
        retry(always, attempts=3, base=1, cap=5, retry_on=(MT5Unavailable,), sleep=slept.append)


@dataclass
class FlakyTerminal:
    up: bool = True
    failures_before_up: int = 0

    def is_connected(self) -> bool:
        return self.up

    def connect(self) -> None:
        if self.failures_before_up > 0:
            self.failures_before_up -= 1
            raise MT5Unavailable("terminal offline")
        self.up = True


def audit_types(engine: Engine) -> list[str]:
    with session_factory(engine).begin() as s:
        return list(s.scalars(select(AuditEvent.event_type).order_by(AuditEvent.id)).all())


def test_monitor_reconnects_and_audits_each_edge_once(engine: Engine) -> None:
    term = FlakyTerminal()
    m = MT5Monitor(term, session_factory(engine), attempts=3, sleep=lambda _: None)
    assert m.ensure()
    assert m.ensure()  # no duplicate CONNECTED
    term.up, term.failures_before_up = False, 2
    assert m.ensure()  # 2 failures then success within 3 attempts
    events = [e for e in audit_types(engine) if e.startswith("MT5")]
    assert events == ["MT5_CONNECTED", "MT5_DISCONNECTED", "MT5_CONNECTED"]


def test_monitor_gives_up_after_bounded_attempts(engine: Engine) -> None:
    term = FlakyTerminal(up=False, failures_before_up=99)
    assert not MT5Monitor(term, session_factory(engine), attempts=3, sleep=lambda _: None).ensure()


# --- runtime ------------------------------------------------------------------------------


def make_runtime(engine: Engine, gw: FakeGateway, sent: list[str]) -> Runtime:
    async def notify(message: str) -> None:
        sent.append(message)

    sessions = session_factory(engine)
    return Runtime(
        settings=load_settings(_env_file=None, **paper_settings()),
        sessions=sessions,
        orchestrator=orchestrator(engine, gw),
        settler=Settler(
            sessions=sessions,
            gateway=gw,
            policy=SettlementPolicy(breakeven_tolerance=Decimal("0.5"), manual_close_is_other=True),
        ),
        monitor=MT5Monitor(gw, sessions, sleep=lambda _: None),  # type: ignore[arg-type]
        notify=notify,
        clock=lambda: NOW,
    )


def test_recover_settles_closed_positions_after_restart(engine: Engine, gw: FakeGateway) -> None:
    open_and_close(engine, gw, "12")  # position closed while "down"
    sent: list[str] = []
    asyncio.run(make_runtime(engine, gw, sent).recover())
    assert any(m.startswith("✅ TRADE CLOSED — WIN") for m in sent)
    assert "APP_STARTED" in audit_types(engine)


def test_recover_flags_crashed_claims_and_never_resends(engine: Engine, gw: FakeGateway) -> None:
    gw.fail_with = RuntimeError("killed mid order_send")
    with pytest.raises(RuntimeError):
        send(orchestrator(engine, gw))
    gw.fail_with = None
    sent: list[str] = []
    asyncio.run(make_runtime(engine, gw, sent).recover())
    assert any("UNRESOLVED EXECUTION" in m for m in sent)
    assert len(gw.orders) == 1
    with session_factory(engine).begin() as s:
        trade = s.scalar(select(Trade))
        assert trade is not None
        assert trade.needs_review


def test_reports_are_sent_once_per_period(engine: Engine, gw: FakeGateway) -> None:
    sent: list[str] = []
    rt = make_runtime(engine, gw, sent)
    assert asyncio.run(rt.send_due_reports()) == ["weekly:2026-09-14", "monthly:2026-08-01"]
    assert asyncio.run(rt.send_due_reports()) == []  # restart-safe via REPORT_GENERATED audit
    assert "WEEKLY REPORT" in sent[0]
    assert "MONTHLY REPORT" in sent[1]


@dataclass
class Msg:
    id: int
    raw_text: str
    date: datetime = NOW


class FakeTelegram:
    def __init__(self, messages: list[Msg]) -> None:
        self.messages = messages

    async def iter_messages(self, entity: int, *, min_id: int, reverse: bool) -> AsyncIterator[Msg]:
        for m in sorted(self.messages, key=lambda m: m.id):
            if m.id > min_id:
                yield m


def test_catch_up_replays_missed_messages_once(engine: Engine, gw: FakeGateway) -> None:
    send(orchestrator(engine, gw), message_id=10)  # last message seen before downtime
    rt = make_runtime(engine, gw, [])
    client = FakeTelegram([Msg(10, GOLD_MSG), Msg(11, GOLD_MSG.replace("4492", "4491"))])
    assert asyncio.run(rt.catch_up(client, SOURCES)) == 1  # only id 11 is new
    assert asyncio.run(rt.catch_up(client, SOURCES)) == 0
    assert len(gw.orders) == 2


def test_first_start_never_trades_channel_history(engine: Engine, gw: FakeGateway) -> None:
    rt = make_runtime(engine, gw, [])
    assert asyncio.run(rt.catch_up(FakeTelegram([Msg(5, GOLD_MSG)]), SOURCES)) == 0
    assert gw.orders == []


def test_status_text(engine: Engine, gw: FakeGateway) -> None:
    rt = make_runtime(engine, gw, [])
    asyncio.run(rt.reconcile())
    text = rt.status_text()
    assert "🩺 SYSTEM STATUS" in text
    assert "MT5:      🟢" in text
    assert "Database: 🟢" in text
    assert "Trading: RUNNING" in text
    assert "12:00:00 UTC" in text


def test_runtime_refuses_to_start_with_open_decisions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    db_url: str,
    tmp_path: Path,
) -> None:
    # cwd is an empty tmp dir (conftest): no real .env; copy only the YAML config.
    (tmp_path / "config").mkdir()
    for name in ("symbols.yaml", "sources.yaml"):
        shutil.copy(ROOT / "config" / name, tmp_path / "config" / name)
    for key, value in {
        "EXECUTION_MODE": "PAPER",
        "DEFAULT_RISK_MODE": "1",
        "EQUITY_LOCK_ENABLED": "false",
        "DATABASE_URL": db_url,
    }.items():
        monkeypatch.setenv(key, value)
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        assert main() == EXIT_CONFIG_ERROR
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
    err = capsys.readouterr().err
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "VOLUME_MAX_POLICY",
        "RISK_ENTRY_SOURCE",
        "BREAKEVEN_TOLERANCE",
        "MANUAL_CLOSE_IS_OTHER",
    ):
        assert name in err


# --- Phase 12 evidence harness ------------------------------------------------------------


def load_acceptance() -> Any:
    spec = importlib.util.spec_from_file_location(
        "demo_acceptance", ROOT / "scripts" / "demo_acceptance.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_acceptance_reports_only_what_the_journal_proves(engine: Engine, gw: FakeGateway) -> None:
    acc = load_acceptance()
    open_and_close(engine, gw, "12")  # Gold BUY with TP1-3, closed at TP
    Settler(
        sessions=session_factory(engine),
        gateway=gw,
        policy=SettlementPolicy(breakeven_tolerance=Decimal("0.5"), manual_close_is_other=True),
    ).reconcile()
    send(orchestrator(engine, gw), text="GOLD BUY NOW\nTP 4510", message_id=2)  # malformed
    with session_factory(engine).begin() as s:
        results = {r.scenario: r for r in acc.collect(s, SYMBOLS)}
        valid = acc.sustained_signals(s)
    assert len(results) == 28
    for passed in (
        "Gold BUY",
        "Gold signal with TP1/TP2/TP3",
        "TP2 selected for Gold",
        "Mode 1 progression",
        "TP closure",
        "Malformed signal",
    ):
        assert results[passed].status == "PASS", passed
    for missing in (
        "Gold SELL",
        "Synthetic BUY",
        "Mode 4 progression",
        "Kill switch",
        "Application restart",
        "Weekly report",
    ):
        assert results[missing].status == "MISSING", missing
    assert results["Shadow Mode reconciliation"].status == "MANUAL"
    assert valid == 1
    report = acc.render(list(results.values()), valid)
    assert "Scenarios with evidence: **6 / 28**" in report
    assert "**1 / 30**" in report


def test_redelivered_message_leaves_duplicate_evidence(engine: Engine, gw: FakeGateway) -> None:
    o = orchestrator(engine, gw)
    send(o)
    assert send(o).state is SignalState.REJECTED_DUPLICATE  # same message delivered again
    with session_factory(engine).begin() as s:
        results = {r.scenario: r for r in load_acceptance().collect(s, SYMBOLS)}
    assert results["Duplicate Telegram message"].status == "PASS"
    assert len(gw.orders) == 1
