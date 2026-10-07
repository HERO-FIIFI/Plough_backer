"""Live runtime: restart recovery (§30), periodic reconciliation, scheduled reports,
missed-message catch-up (§58) and health status (§56).

`Runtime` holds the logic and takes every dependency as an argument (testable with fakes);
`run()` is the thin wiring to MT5, Telethon and python-telegram-bot.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from plough_backer import SPEC_VERSION, controls
from plough_backer.analytics.reports import (
    monthly_report,
    previous_month,
    previous_week,
    weekly_report,
)
from plough_backer.config import Settings, SourcesConfig
from plough_backer.enums import AuditEventType, ExecutionMode, RiskMode, TradingStatus
from plough_backer.persistence import repositories as repo
from plough_backer.persistence.models import AuditEvent, Signal, Trade
from plough_backer.resilience import MemoryPressureMonitor, MT5Monitor
from plough_backer.telegram.notifications import trade_closed
from plough_backer.trading.executor import MultiAccountOutcome, Orchestrator
from plough_backer.trading.settlement import Settler

log = logging.getLogger(__name__)
Notify = Callable[[str], Awaitable[None]]


class MessageSource(Protocol):
    """The slice of Telethon's client used for catch-up."""

    def iter_messages(self, entity: int, *, min_id: int, reverse: bool) -> AsyncIterator[Any]: ...


@dataclass
class Health:
    telegram: bool
    mt5: bool
    database: bool
    scheduler: bool
    trading: TradingStatus
    last_signal_at: datetime | None
    last_reconciliation_at: datetime | None


class Runtime:
    def __init__(
        self,
        *,
        settings: Settings,
        sessions: sessionmaker[Session],
        orchestrator: Orchestrator,
        settler: Settler | None,
        monitor: MT5Monitor | None,
        notify: Notify,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        memory_monitor: MemoryPressureMonitor | None = None,
        account_services: dict[str, tuple[MT5Monitor, Settler]] | None = None,
    ) -> None:
        self._settings, self._sessions = settings, sessions
        self._orchestrator, self._settler, self._monitor = orchestrator, settler, monitor
        self._notify, self._now = notify, clock
        self._memory_monitor = memory_monitor or MemoryPressureMonitor()
        self._account_services = account_services if account_services is not None else {}
        self.last_reconciliation_at: datetime | None = None
        self.scheduler_alive = False
        self.telegram_connected: Callable[[], bool] = lambda: False

    # --- §30 restart recovery -------------------------------------------------------------

    async def recover(self) -> None:
        """Steps 4-10: MT5, config, unresolved executions, reconcile, settle idempotently."""
        with self._sessions.begin() as s:
            controls.seed_from_env(s, self._settings)
            repo.append_audit(
                s,
                AuditEventType.APP_STARTED,
                spec_version=SPEC_VERSION,
                execution_mode=self._settings.execution_mode,
            )
        for trade_id, signal_id in self.unresolved_claims():
            # Crashed between claim and broker response: never resubmitted (INV-15).
            await self._notify(
                f"🔎 UNRESOLVED EXECUTION\n\nSignal: {signal_id}\nTrade #{trade_id}\n\n"
                "The app stopped after requesting this order and before MT5 answered.\n"
                "It will NOT be resent. Check MT5 manually."
            )
        await self.reconcile()

    def unresolved_claims(self) -> list[tuple[int, str]]:
        with self._sessions.begin() as s:
            rows = s.execute(
                select(Trade.id, Trade.signal_id).where(
                    Trade.broker_response_at.is_(None), Trade.settled_at.is_(None)
                )
            ).all()
            for trade_id, _ in rows:
                trade = s.get(Trade, trade_id)
                assert trade is not None
                trade.needs_review = True
        return [(t, sid) for t, sid in rows]

    # --- periodic jobs --------------------------------------------------------------------

    async def reconcile(self) -> None:
        if self._account_services:
            for account_id, (monitor, settler) in list(self._account_services.items()):
                connected = await asyncio.to_thread(monitor.ensure)
                if not connected:
                    continue
                settlements = await asyncio.to_thread(settler.reconcile)
                for settlement in settlements:
                    await self._notify(
                        f"Account: {account_id}\n\n"
                        f"{trade_closed(settlement, RiskMode(settlement.risk_mode))}"
                    )
            self.last_reconciliation_at = self._now()
            return
        if self._settler is None or self._monitor is None:
            return
        connected = await asyncio.to_thread(self._monitor.ensure)
        if not connected:
            return
        settlements = await asyncio.to_thread(self._settler.reconcile)
        self.last_reconciliation_at = self._now()
        if settlements:
            with self._sessions.begin() as s:
                mode = controls.risk_mode(s)
            for st in settlements:
                await self._notify(trade_closed(st, mode))

    async def send_due_reports(self) -> list[str]:
        """Weekly/monthly reports for the last completed period, each exactly once (§27)."""
        now = self._now()
        today = now.astimezone(_tz(self._settings.report_timezone)).date()
        sent = []
        for kind, period, builder in (
            ("weekly", previous_week, weekly_report),
            ("monthly", previous_month, monthly_report),
        ):
            start, end = period(today, self._settings.report_timezone)
            key = f"{kind}:{start.date().isoformat()}"
            with self._sessions.begin() as s:
                if self._report_done(s, key):
                    continue
                report = builder(s, start, end, controls.risk_mode(s))
                repo.append_audit(s, AuditEventType.REPORT_GENERATED, report=key)
            await self._notify(report)
            sent.append(key)
        return sent

    @staticmethod
    def _report_done(s: Session, key: str) -> bool:
        events: Sequence[dict[str, Any]] = s.scalars(
            select(AuditEvent.payload).where(
                AuditEvent.event_type == AuditEventType.REPORT_GENERATED
            )
        ).all()
        return any(p.get("report") == key for p in events)

    # --- §58 catch-up ---------------------------------------------------------------------

    async def catch_up(self, client: MessageSource, sources: SourcesConfig) -> int:
        """Replay messages posted while we were down. Dedup makes this safe (INV-01).

        A source with no recorded message is skipped: first start must not trade history.
        """
        processed = 0
        for source in sources.sources:
            if not source.enabled:
                continue
            with self._sessions.begin() as s:
                last_id = s.scalar(
                    select(func.max(Signal.source_message_id)).where(Signal.source_id == source.id)
                )
            if last_id is None:
                continue
            async for msg in client.iter_messages(
                source.telegram_chat_id, min_id=last_id, reverse=True
            ):
                outcome = await asyncio.to_thread(
                    self._orchestrator.process_message,
                    source_id=source.id,
                    message_id=msg.id,
                    message_time=msg.date,
                    text=msg.raw_text or "",
                    received_at=self._now(),
                )
                processed += 1
                log.info("catch_up_message", extra={"source": source.id, "state": outcome.state})
        return processed

    # --- §56 health -----------------------------------------------------------------------

    def health(self) -> Health:
        database = True
        try:
            with self._sessions.begin() as s:  # BEGIN IMMEDIATE proves the DB is writable
                s.execute(text("SELECT 1"))
                status = controls.trading_status(s)
                last_signal = s.scalar(select(func.max(Signal.received_at)))
        except Exception:
            database, status, last_signal = False, TradingStatus.STOPPED, None
        return Health(
            telegram=self.telegram_connected(),
            mt5=(
                all(bool(monitor.connected) for monitor, _ in self._account_services.values())
                if self._account_services
                else bool(self._monitor and self._monitor.connected)
            ),
            database=database,
            scheduler=self.scheduler_alive,
            trading=status,
            last_signal_at=last_signal,
            last_reconciliation_at=self.last_reconciliation_at,
        )

    def status_text(self) -> str:
        h = self.health()
        dot = {True: "🟢", False: "🔴"}
        last = f"{h.last_reconciliation_at:%H:%M:%S} UTC" if h.last_reconciliation_at else "—"
        signal = f"{h.last_signal_at:%Y-%m-%d %H:%M:%S} UTC" if h.last_signal_at else "—"
        return (
            f"🩺 SYSTEM STATUS\n\nTelegram: {dot[h.telegram]}\nMT5:      {dot[h.mt5]}\n"
            f"Database: {dot[h.database]}\nScheduler:{dot[h.scheduler]}\n\n"
            f"Mode: {self._settings.execution_mode}\nTrading: {h.trading}\n\n"
            f"Last Signal:\n{signal}\n\nLast Reconciliation:\n{last}"
        )

    async def periodic(self) -> None:
        """Reconciliation every N seconds; report check each loop. Errors never kill the loop."""
        self.scheduler_alive = True
        try:
            while True:
                for job in (self.reconcile, self.send_due_reports, self.check_memory):
                    try:
                        await job()
                    except Exception:
                        log.exception("periodic_job_failed", extra={"job": job.__name__})
                await asyncio.sleep(self._settings.reconcile_interval_seconds)
        finally:
            self.scheduler_alive = False

    async def check_memory(self) -> None:
        """Send only threshold transitions; repeated high readings stay quiet."""
        message = self._memory_monitor.sample()
        if message is not None:
            await self._notify(f"⚠️ {message}")


def _tz(name: str) -> Any:
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


async def run(settings: Settings, sessions: sessionmaker[Session]) -> None:  # pragma: no cover
    """Wire real MT5 + Telegram. Not unit-tested: needs credentials and a terminal (Phase 12)."""
    from plough_backer.account_credentials import (
        CredentialVault,
        SetupLinkService,
        WindowsDPAPIProtector,
    )
    from plough_backer.config import (
        load_account_environment,
        load_accounts,
        load_sources,
        load_symbols,
        resolve_account_credentials,
    )
    from plough_backer.credential_server import CredentialSetupServer
    from plough_backer.exceptions import ConfigurationError
    from plough_backer.telegram import notifications
    from plough_backer.telegram.bot import build_application
    from plough_backer.telegram.handlers import AdminCommands
    from plough_backer.telegram.listener import build_listener, warm_entities
    from plough_backer.trading.executor import (
        ExecutionPolicy,
        MultiAccountOrchestrator,
        SignalIngestor,
    )
    from plough_backer.trading.mt5_client import MT5Client, load_mt5
    from plough_backer.trading.process_gateway import MT5WorkerSpec, ProcessBrokerGateway
    from plough_backer.trading.settlement import SettlementPolicy

    # Type narrowing only: main() already refused to start if runtime_missing() was non-empty.
    assert settings.telegram_api_id is not None
    assert settings.telegram_api_hash is not None
    assert settings.telegram_bot_token is not None
    assert settings.telegram_admin_user_id is not None
    assert settings.volume_max_policy is not None
    assert settings.risk_entry_source is not None
    assert settings.breakeven_tolerance is not None
    assert settings.manual_close_is_other is not None
    symbols, sources, accounts = load_symbols(), load_sources(), load_accounts()
    account_environment = load_account_environment(os.environ)
    names = {s.id: s.name for s in sources.sources}

    gateway = monitor = settler = None
    gateways: dict[str, Any] = {}
    account_services: dict[str, tuple[MT5Monitor, Settler]] = {}
    methods: dict[str, RiskMode] = {}
    setup_server: CredentialSetupServer | None = None
    credential_setup: SetupLinkService | None = None
    vault: CredentialVault | None = None
    account_dashboard: dict[str, tuple[Any, RiskMode]] = {}
    policy = ExecutionPolicy(
        execution_mode=settings.execution_mode,
        progression_scope=settings.progression_scope,
        volume_max_policy=settings.volume_max_policy,
        risk_entry=settings.risk_entry_source,
    )
    if accounts.enabled_masters:
        executors: dict[str, Orchestrator] = {}
        vault = CredentialVault(sessions, WindowsDPAPIProtector())

        def configured_credentials(account: Any) -> Any:
            saved = vault.load(account.id)
            if saved is not None:
                return saved
            try:
                return resolve_account_credentials(account, account_environment)
            except ConfigurationError:
                return None

        def account_executor(account: Any, account_gateway: Any) -> Orchestrator:
            return Orchestrator(
                sessions=sessions,
                gateway=account_gateway,
                symbols=symbols,
                sources=sources,
                policy=policy,
                account_id=account.id,
                risk_mode=account.method,
            )

        for account in accounts.enabled_masters:
            methods[account.id] = account.method
            credentials = configured_credentials(account)
            if credentials is None or settings.execution_mode is ExecutionMode.PAPER:
                executors[account.id] = account_executor(account, None)
                continue
            assert account.deviation_points is not None
            assert account.magic is not None
            isolated_gateway = ProcessBrokerGateway(
                MT5WorkerSpec(
                    account_id=account.id,
                    login=credentials.login,
                    password=credentials.password.get_secret_value(),
                    server=credentials.server,
                    deviation_points=account.deviation_points,
                    magic=account.magic,
                    terminal_path=account.terminal_path,
                )
            )
            account_monitor = MT5Monitor(isolated_gateway, sessions)
            account_settler = Settler(
                sessions=sessions,
                gateway=isolated_gateway,
                account_id=account.id,
                policy=SettlementPolicy(
                    breakeven_tolerance=settings.breakeven_tolerance,
                    manual_close_is_other=settings.manual_close_is_other,
                ),
            )
            gateways[account.id] = isolated_gateway
            account_dashboard[account.id] = (isolated_gateway, account.method)
            account_services[account.id] = (account_monitor, account_settler)
            executors[account.id] = account_executor(account, isolated_gateway)
        orchestrator: Any = MultiAccountOrchestrator(
            ingestor=SignalIngestor(sessions=sessions, symbols=symbols, sources=sources),
            executors=executors,
        )
        gateway = next(iter(gateways.values()), None)

        accounts_by_id = {account.id: account for account in accounts.enabled_masters}

        def activate_account(account_id: str) -> None:
            account = accounts_by_id[account_id]
            credentials = vault.load(account_id)
            if credentials is None or settings.execution_mode is ExecutionMode.PAPER:
                return
            assert settings.breakeven_tolerance is not None
            assert settings.manual_close_is_other is not None
            assert account.deviation_points is not None
            assert account.magic is not None
            replacement = ProcessBrokerGateway(
                MT5WorkerSpec(
                    account_id=account.id,
                    login=credentials.login,
                    password=credentials.password.get_secret_value(),
                    server=credentials.server,
                    deviation_points=account.deviation_points,
                    magic=account.magic,
                    terminal_path=account.terminal_path,
                )
            )
            account_monitor = MT5Monitor(replacement, sessions)
            account_settler = Settler(
                sessions=sessions,
                gateway=replacement,
                account_id=account.id,
                policy=SettlementPolicy(
                    breakeven_tolerance=settings.breakeven_tolerance,
                    manual_close_is_other=settings.manual_close_is_other,
                ),
            )
            previous = gateways.get(account_id)
            gateways[account_id] = replacement
            account_dashboard[account_id] = (replacement, account.method)
            account_services[account_id] = (account_monitor, account_settler)
            orchestrator.replace_executor(
                account_id, account_executor(account, replacement)
            )
            if previous is not None:
                previous.shutdown()

        credential_setup = SetupLinkService(
            sessions=sessions,
            vault=vault,
            base_url=settings.account_setup_base_url,
            allowed_account_ids=set(accounts_by_id),
            on_saved=activate_account,
        )
        setup_server = CredentialSetupServer(
            credential_setup,
            host=settings.account_setup_host,
            port=settings.account_setup_port,
            tls_cert=settings.account_setup_tls_cert,
            tls_key=settings.account_setup_tls_key,
        )
        setup_server.start()
    elif settings.mt5_login and settings.mt5_password and settings.mt5_server:
        assert settings.mt5_deviation_points is not None
        assert settings.mt5_magic is not None
        gateway = MT5Client(
            load_mt5(),
            login=settings.mt5_login,
            password=settings.mt5_password,
            server=settings.mt5_server,
            deviation_points=settings.mt5_deviation_points,
            magic=settings.mt5_magic,
            terminal_path=settings.mt5_terminal_path,
        )
        monitor = MT5Monitor(gateway, sessions)
        settler = Settler(
            sessions=sessions,
            gateway=gateway,
            policy=SettlementPolicy(
                breakeven_tolerance=settings.breakeven_tolerance,
                manual_close_is_other=settings.manual_close_is_other,
            ),
        )
        await asyncio.to_thread(monitor.ensure)

    if not accounts.enabled_masters:
        orchestrator = Orchestrator(
            sessions=sessions,
            gateway=gateway,
            symbols=symbols,
            sources=sources,
            policy=policy,
        )
    admin_id = settings.telegram_admin_user_id
    runtime_ref: list[Runtime] = []
    commands = AdminCommands(
        sessions=sessions,
        gateway=gateway,
        admin_ids={admin_id},
        execution_mode=settings.execution_mode,
        status=lambda: runtime_ref[0].status_text(),
        account_gateways=account_dashboard,
        master_accounts=accounts.enabled_masters,
        credential_setup=credential_setup,
        credential_present=vault.has if vault is not None else None,
    )
    bot = build_application(settings.telegram_bot_token.get_secret_value(), commands)

    async def notify(message: str) -> None:
        try:
            for part in notifications.split_message(message):
                await bot.bot.send_message(chat_id=admin_id, text=part)
        except Exception:
            log.exception("notify_failed")  # never let Telegram break trading

    runtime = Runtime(
        settings=settings,
        sessions=sessions,
        orchestrator=orchestrator,
        settler=settler,
        monitor=monitor,
        notify=notify,
        account_services=account_services,
    )
    runtime_ref.append(runtime)

    async def on_outcome(source_id: str, outcome: Any) -> None:
        if isinstance(outcome, MultiAccountOutcome):
            if outcome.intake is not None:
                message = notifications.for_outcome(
                    outcome.intake, names.get(source_id, source_id), settings.default_risk_mode
                )
                if message:
                    await notify(message)
            for account_id, account_outcome in outcome.accounts.items():
                message = notifications.for_outcome(
                    account_outcome,
                    names.get(source_id, source_id),
                    methods[account_id],
                )
                if message:
                    await notify(f"Account: {account_id}\n\n{message}")
            return
        with sessions.begin() as s:
            mode = controls.risk_mode(s)
        message = notifications.for_outcome(outcome, names.get(source_id, source_id), mode)
        if message:
            await notify(message)

    async def on_edit(edit: Any) -> None:
        await notify(notifications.message_edited(edit))

    listener = build_listener(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash.get_secret_value(),
        session_path=settings.telegram_session_path,
        sources=sources,
        orchestrator=orchestrator,
        on_outcome=on_outcome,
        on_edit=on_edit,
    )
    runtime.telegram_connected = listener.is_connected

    await bot.initialize()
    await bot.start()
    assert bot.updater is not None
    await bot.updater.start_polling(drop_pending_updates=True)  # stale presses must not apply
    await listener.start()  # Telethon: built-in reconnect with retries
    missing = await warm_entities(listener, sources)
    if missing:
        log.error("telegram_chats_not_joined", extra={"chat_ids": missing})
        await notify(f"⚠️ Listener account can't see source chats: {missing}. Join them.")
    with sessions.begin() as s:
        repo.append_audit(s, AuditEventType.TELEGRAM_CONNECTED)
    await runtime.recover()
    await runtime.catch_up(listener, sources)
    periodic = asyncio.create_task(runtime.periodic())
    await notify(runtime.status_text())
    try:
        await listener.run_until_disconnected()
    finally:
        periodic.cancel()
        with sessions.begin() as s:
            repo.append_audit(s, AuditEventType.APP_STOPPED)
        await bot.updater.stop()
        await bot.stop()
        await bot.shutdown()
        if setup_server is not None:
            setup_server.stop()
        if gateways:
            for isolated_gateway in gateways.values():
                isolated_gateway.shutdown()
        elif gateway is not None:
            gateway.shutdown()
