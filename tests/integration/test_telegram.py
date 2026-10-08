"""Phase 7: admin bot logic, notifications, edit policy, and offline library wiring."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from plough_backer import controls
from plough_backer.config import TradingAccountConfig, load_accounts
from plough_backer.enums import (
    AuditEventType,
    ExecutionMode,
    RiskMode,
    SignalState,
    TradingStatus,
)
from plough_backer.exceptions import BrokerRejected
from plough_backer.persistence.database import PROJECT_ROOT, session_factory
from plough_backer.persistence.models import AuditEvent, Signal
from plough_backer.telegram import notifications as n
from plough_backer.telegram.handlers import NOT_AUTHORIZED, AdminCommands
from tests.fakes import FakeGateway
from tests.integration.test_orchestrator import GOLD_MSG, SOURCES, orchestrator, send

ADMIN, STRANGER = 42, 666


@pytest.fixture
def admin(engine: Engine, gw: FakeGateway) -> AdminCommands:
    return AdminCommands(
        sessions=session_factory(engine),
        gateway=gw,
        admin_ids={ADMIN},
        execution_mode=ExecutionMode.DEMO,
    )


def audit_types(engine: Engine) -> list[str]:
    with session_factory(engine).begin() as s:
        return list(s.scalars(select(AuditEvent.event_type).order_by(AuditEvent.id)).all())


# --- authorization (§54) ------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    ["dashboard", "mode_menu", "lock_menu", "pause", "stop", "mode_confirm", "lock_confirm"],
)
def test_unauthorized_users_get_nothing_and_change_nothing(
    admin: AdminCommands,
    engine: Engine,
    action: str,
) -> None:
    calls = {
        "dashboard": lambda: admin.dashboard(STRANGER),
        "mode_menu": lambda: admin.mode_menu(STRANGER),
        "lock_menu": lambda: admin.lock_menu(STRANGER),
        "pause": lambda: admin.callback(STRANGER, "status:PAUSED"),
        "stop": lambda: admin.callback(STRANGER, "status:STOPPED"),
        "mode_confirm": lambda: admin.callback(STRANGER, "mode:confirm:1:4"),
        "lock_confirm": lambda: admin.callback(STRANGER, "lock:confirm:900"),
    }
    assert calls[action]() == NOT_AUTHORIZED
    with session_factory(engine).begin() as s:
        assert controls.trading_status(s) is TradingStatus.RUNNING
        assert controls.risk_mode(s) is RiskMode.ANTI_MARTINGALE
        assert controls.equity_lock(s) == (False, None)
    assert audit_types(engine) == []


def test_missing_user_id_is_unauthorized(admin: AdminCommands) -> None:
    assert admin.dashboard(None) == NOT_AUTHORIZED


def test_admin_ids_required(engine: Engine) -> None:
    with pytest.raises(ValueError, match="admin"):
        AdminCommands(
            sessions=session_factory(engine),
            gateway=None,
            admin_ids=set(),
            execution_mode=ExecutionMode.DEMO,
        )


# --- dashboard ----------------------------------------------------------------------------


def test_dashboard_shows_account_and_method(admin: AdminCommands) -> None:
    reply = admin.dashboard(ADMIN)
    assert "🚜 PLOUGH BACKER" in reply.text
    assert "$800.00" in reply.text
    assert "1️⃣ Anti-Martingale" in reply.text
    assert "🟢 Connected" in reply.text
    labels = [label for row in reply.buttons for label, _ in row]
    assert "⏸ Pause" in labels
    assert "⛔ Stop" in labels


# --- mode change with confirmation (§32, §33) ---------------------------------------------


def test_dashboard_lists_every_direct_master(engine: Engine) -> None:
    first, second = FakeGateway(), FakeGateway()
    second.account = replace(second.account, balance=Decimal("60"), equity=Decimal("59"))
    commands = AdminCommands(
        sessions=session_factory(engine),
        gateway=first,
        admin_ids={ADMIN},
        execution_mode=ExecutionMode.DEMO,
        account_gateways={
            "method-1-primary": (first, RiskMode.ANTI_MARTINGALE),
            "method-2-primary": (second, RiskMode.MARTINGALE),
        },
    )

    text = commands.dashboard(ADMIN).text

    assert "method-1-primary | Method 1" in text
    assert "method-2-primary | Method 2" in text
    assert "Balance $60.00" in text


def test_accounts_dashboard_issues_password_link_only_for_authorized_admin(
    engine: Engine,
) -> None:
    class Setup:
        def issue(self, account_id: str, requested_by: int) -> str:
            assert requested_by == ADMIN
            return f"https://setup.example.com/setup/token-for-{account_id}"

    accounts = load_accounts(PROJECT_ROOT / "config/accounts.yaml")
    account = TradingAccountConfig.model_validate(
        {
            "id": "method-1-primary",
            "name": "Method 1 Primary",
            "method": 1,
            "role": "MASTER",
            "terminal_path": r"C:\MT5\Method1\terminal64.exe",
            "credentials_prefix": "MT5_METHOD1",
            "magic": 71001,
            "deviation_points": 20,
        }
    )
    commands = AdminCommands(
        sessions=session_factory(engine),
        gateway=None,
        admin_ids={ADMIN},
        execution_mode=ExecutionMode.DEMO,
        master_accounts=[*accounts.enabled_masters, account],
        credential_setup=Setup(),
    )

    menu = commands.accounts(ADMIN)
    assert "method-1-primary" in menu.text
    assert menu.buttons == [[("Set credentials", "account:setup:method-1-primary")]]
    assert [("Accounts", "account:menu")] in commands.dashboard(ADMIN).buttons
    assert commands.callback(ADMIN, "account:menu") == menu
    link = commands.callback(ADMIN, "account:setup:method-1-primary")
    assert "https://setup.example.com/setup/token-for-method-1-primary" in link.text
    assert commands.accounts(STRANGER) == NOT_AUTHORIZED


def test_mode_change_needs_confirmation(admin: AdminCommands, engine: Engine) -> None:
    menu = admin.callback(ADMIN, "mode:menu")
    assert "Current:\n1️⃣ Anti-Martingale" in menu.text
    ask = admin.callback(ADMIN, "mode:ask:1:5")
    assert "5️⃣ ×2 Every 5 Trades?" in ask.text
    with session_factory(engine).begin() as s:
        assert controls.risk_mode(s) is RiskMode.ANTI_MARTINGALE  # not yet
    confirm_data = ask.buttons[0][0][1]
    assert confirm_data == "mode:confirm:1:5"
    admin.callback(ADMIN, confirm_data)
    with session_factory(engine).begin() as s:
        assert controls.risk_mode(s) is RiskMode.DOUBLE_EVERY_FIVE
    assert AuditEventType.MODE_CHANGED in audit_types(engine)


def test_stale_confirmation_button_is_refused(admin: AdminCommands, engine: Engine) -> None:
    admin.callback(ADMIN, "mode:confirm:1:5")  # now mode 5
    reply = admin.callback(ADMIN, "mode:confirm:1:4")  # old button still says "from 1"
    assert "nothing applied" in reply.text
    with session_factory(engine).begin() as s:
        assert controls.risk_mode(s) is RiskMode.DOUBLE_EVERY_FIVE


# --- Equity Lock (§39) --------------------------------------------------------------------


def test_equity_lock_enable_and_disable_with_confirmation(
    admin: AdminCommands,
    engine: Engine,
) -> None:
    ask = admin.lock_ask(ADMIN, "$500.00")
    assert ask.buttons[0][0][1] == "lock:confirm:500.00"
    admin.callback(ADMIN, "lock:confirm:500.00")
    with session_factory(engine).begin() as s:
        assert controls.equity_lock(s) == (True, Decimal("500.00"))
    assert "ENABLED" in admin.lock_menu(ADMIN).text
    admin.callback(ADMIN, "lock:confirm_disable")
    with session_factory(engine).begin() as s:
        assert controls.equity_lock(s) == (False, None)
    assert audit_types(engine).count(AuditEventType.EQUITY_LOCK_CHANGED) == 2


@pytest.mark.parametrize("bad", ["abc", "-5", "NaN"])
def test_equity_lock_rejects_bad_amounts(admin: AdminCommands, bad: str) -> None:
    assert admin.lock_ask(ADMIN, bad).buttons == []


# --- pause / resume / stop drive the orchestrator (§34, §35) ------------------------------


def test_pause_resume_stop(admin: AdminCommands, engine: Engine, gw: FakeGateway) -> None:
    o = orchestrator(engine, gw)
    admin.callback(ADMIN, "status:PAUSED")
    assert send(o, message_id=1).state is SignalState.SKIPPED_PAUSED
    admin.callback(ADMIN, "status:RUNNING")
    assert send(o, message_id=2).state is SignalState.OPEN
    reply = admin.callback(ADMIN, "status:STOPPED")
    assert "NOT closed" in reply.text  # §35: kill switch never closes positions
    assert send(o, message_id=3).state is SignalState.SKIPPED_STOPPED
    assert len(gw.orders) == 1
    types = audit_types(engine)
    assert AuditEventType.PAUSED in types
    assert AuditEventType.RESUMED in types
    assert AuditEventType.KILL_SWITCH in types


# --- notifications ------------------------------------------------------------------------


def test_trade_executed_notification(engine: Engine, gw: FakeGateway) -> None:
    out = send(orchestrator(engine, gw))
    text = n.for_outcome(out, "Gold Signals", RiskMode.ANTI_MARTINGALE)
    assert text is not None
    assert text.startswith("🚜 TRADE EXECUTED")
    for part in (
        "XAUUSD BUY",
        "Source: Gold Signals",
        "TP:    4512",
        "Theoretical Lot: 0.0100",
        "Executed Lot:    0.01",
        "Risk to SL:      $8.00",
        "Actual RR:       1:1.50",
        "Signal: PB-000001",
    ):
        assert part in text


def test_insufficient_margin_retry_notification_explains_reset(
    engine: Engine, gw: FakeGateway
) -> None:
    from plough_backer.persistence import repositories as repo
    from plough_backer.risk.progression import ProgressionState
    from tests.integration.test_orchestrator import KEY

    with session_factory(engine).begin() as s:
        repo.create_progression(
            s,
            KEY,
            ProgressionState(
                mode=RiskMode.ANTI_MARTINGALE,
                base_lot=Decimal("0.01"),
                theoretical_lot=Decimal("0.16"),
                wins=4,
            ),
        )
    gw.failures = [BrokerRejected(10019, "No money")]

    out = send(orchestrator(engine, gw))
    text = n.for_outcome(out, "Gold Signals", RiskMode.ANTI_MARTINGALE)

    assert text is not None
    assert "Insufficient margin: progression reset from 0.1600 to 0.0100." in text
    assert "Order retried once at 0.01 lot and executed." in text


def test_equity_lock_notification_explains_margin_reset_without_retry(
    engine: Engine, gw: FakeGateway
) -> None:
    from plough_backer.persistence import repositories as repo
    from plough_backer.risk.progression import ProgressionState
    from tests.integration.test_orchestrator import KEY

    with session_factory(engine).begin() as s:
        repo.create_progression(
            s,
            KEY,
            ProgressionState(
                mode=RiskMode.ANTI_MARTINGALE,
                base_lot=Decimal("0.01"),
                theoretical_lot=Decimal("0.16"),
                wins=4,
            ),
        )
        controls.set_equity_lock(s, enabled=True, value=Decimal("20"), actor="42")
    gw.failures = [BrokerRejected(10019, "No money")]
    gw.account_snapshots = [
        gw.account,
        replace(
            gw.account,
            balance=Decimal("25"),
            equity=Decimal("25"),
            free_margin=Decimal("25"),
        ),
    ]

    out = send(orchestrator(engine, gw))
    text = n.for_outcome(out, "Gold Signals", RiskMode.ANTI_MARTINGALE)

    assert text is not None
    assert "Progression reset from 0.1600 to 0.0100." in text
    assert "Retry blocked by Equity Lock." in text
    assert "Progression unchanged." not in text


def test_invalid_signal_notification_matches_readme(engine: Engine, gw: FakeGateway) -> None:
    out = send(orchestrator(engine, gw), text="GOLD BUY NOW\nTP 4510")
    text = n.for_outcome(out, "Gold Signals", RiskMode.ANTI_MARTINGALE)
    assert text == (
        "⚠️ SIGNAL NOT EXECUTED\n\nSource: Gold Signals\nSymbol: —\n"
        "Reason: Stop loss could not be determined.\n\nMessage recorded for review."
    )


def test_equity_lock_notification(engine: Engine, gw: FakeGateway) -> None:
    with session_factory(engine).begin() as s:
        controls.set_equity_lock(s, enabled=True, value=Decimal(795), actor="42")
    out = send(orchestrator(engine, gw))
    text = n.for_outcome(out, "Gold Signals", RiskMode.ANTI_MARTINGALE)
    assert text is not None
    assert "🔒 TRADE BLOCKED — EQUITY LOCK" in text
    assert "Projected Equity: $792.00" in text
    assert "Progression unchanged." in text


def test_duplicates_are_not_pushed(engine: Engine, gw: FakeGateway) -> None:
    o = orchestrator(engine, gw)
    send(o)
    assert n.for_outcome(send(o), "Gold Signals", RiskMode.ANTI_MARTINGALE) is None


@pytest.mark.parametrize(
    ("value", "text"),
    [(Decimal("0.04"), "0.0400"), (Decimal("0.00075"), "0.00075"), (Decimal("0.5"), "0.5000")],
)
def test_lot_formatting_keeps_deriv_precision(value: Decimal, text: str) -> None:
    assert n.lot(value) == text


# --- edits (§25) --------------------------------------------------------------------------


def test_edit_after_execution_alerts_and_changes_nothing(
    engine: Engine,
    gw: FakeGateway,
) -> None:
    o = orchestrator(engine, gw)
    send(o)
    edit = o.process_edit(
        source_id="gold",
        message_id=1,
        text=GOLD_MSG.replace("4492", "4490"),
        edited_at=datetime.now(UTC),
    )
    assert edit is not None
    assert edit.executed
    assert "SOURCE MESSAGE EDITED" in n.message_edited(edit)
    assert len(gw.orders) == 1
    with session_factory(engine).begin() as s:
        signal = s.scalar(select(Signal))
        assert signal is not None
        assert signal.raw_message == GOLD_MSG  # original kept
        assert signal.edited_message is not None
        assert "4490" in signal.edited_message


def test_edit_of_unknown_message_is_ignored(engine: Engine, gw: FakeGateway) -> None:
    o = orchestrator(engine, gw)
    assert (
        o.process_edit(source_id="gold", message_id=99, text="x", edited_at=datetime.now(UTC))
        is None
    )


# --- library wiring (offline: nothing connects) -------------------------------------------


def test_bot_application_builds_with_all_commands(admin: AdminCommands) -> None:
    from telegram.ext import CallbackQueryHandler, CommandHandler

    from plough_backer.telegram.bot import build_application

    app = build_application("123456:TEST-TOKEN-NOT-REAL", admin)
    handlers = [h for group in app.handlers.values() for h in group]
    commands = {c for h in handlers if isinstance(h, CommandHandler) for c in h.commands}
    assert commands == {
        "start",
        "dashboard",
        "status",
        "accounts",
        "mode",
        "lock",
        "pause",
        "resume",
        "stop",
    }
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)


def test_listener_builds_for_enabled_sources(
    engine: Engine,
    gw: FakeGateway,
    tmp_path: Path,
) -> None:
    from plough_backer.telegram.listener import build_listener, chat_map

    async def noop(*_: object) -> None: ...

    client = build_listener(
        api_id=1,
        api_hash="0" * 32,
        session_path=str(tmp_path / "listener"),
        sources=SOURCES,
        orchestrator=orchestrator(engine, gw),
        on_outcome=noop,
        on_edit=noop,
    )
    assert chat_map(SOURCES) == {-100: "gold"}
    assert len(client.list_event_handlers()) == 2
    assert not client.is_connected()


def test_split_message_fits_telegram_cap() -> None:
    assert n.split_message("short") == ["short"]
    assert n.split_message("") == [""]
    text = "\n".join(f"line {i}" for i in range(2000))  # ~17k chars
    parts = n.split_message(text)
    assert all(len(p) <= n.TELEGRAM_MAX_CHARS for p in parts)
    assert "".join(parts) == text  # nothing lost, splits on line breaks
    long_line = "x" * 10_000
    assert [len(p) for p in n.split_message(long_line)] == [4096, 4096, 1808]


def test_warm_entities_reports_unjoined_chats() -> None:
    import asyncio

    from plough_backer.config import SourcesConfig
    from plough_backer.telegram.listener import warm_entities

    src = {"name": "x", "parser_profile": "standard_v1"}
    sources = SourcesConfig.model_validate(
        {
            "sources": [
                {**src, "id": "joined", "telegram_chat_id": -100},
                {**src, "id": "not_joined", "telegram_chat_id": -200},
            ]
        }
    )

    class FakeClient:
        dialogs_loaded = False

        async def get_dialogs(self) -> None:
            self.dialogs_loaded = True

        async def get_entity(self, chat_id: int) -> object:
            if not self.dialogs_loaded or chat_id != -100:
                raise ValueError("Could not find the input entity")
            return object()

    assert asyncio.run(warm_entities(FakeClient(), sources)) == [-200]  # type: ignore[arg-type]
