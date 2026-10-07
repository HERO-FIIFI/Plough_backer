"""Admin bot logic (README §31–§35, §39, §54), independent of python-telegram-bot.

Every entry point checks authorization first; unauthorized users get no data and cause
no change. Confirmations are stateless: the callback data carries the value it confirms
AND the value it expects to replace, so a stale button can't apply an outdated change.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from plough_backer import controls
from plough_backer.config import TradingAccountConfig
from plough_backer.enums import ExecutionMode, RiskMode, TradeOutcome, TradingStatus
from plough_backer.exceptions import BrokerError
from plough_backer.persistence.models import ProgressionStateRow, Trade
from plough_backer.telegram.notifications import lot, mode_label, money
from plough_backer.trading.gateway import BrokerGateway

log = logging.getLogger(__name__)

Button = tuple[str, str]  # (label, callback_data)


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)


NOT_AUTHORIZED = Reply("⛔ Not authorized.")


class CredentialSetup(Protocol):
    def issue(self, account_id: str, requested_by: int) -> str: ...


class AdminCommands:
    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        gateway: BrokerGateway | None,
        admin_ids: set[int],
        execution_mode: ExecutionMode,
        status: Callable[[], str] | None = None,
        account_gateways: dict[str, tuple[BrokerGateway, RiskMode]] | None = None,
        master_accounts: list[TradingAccountConfig] | None = None,
        credential_setup: CredentialSetup | None = None,
        credential_present: Callable[[str], bool] | None = None,
    ) -> None:
        if not admin_ids:
            raise ValueError("at least one Telegram admin id is required (§54)")
        self._status = status
        self._sessions = sessions
        self._gateway = gateway
        self._admins = admin_ids
        self._mode = execution_mode
        self._account_gateways = account_gateways if account_gateways is not None else {}
        self._master_accounts = {account.id: account for account in master_accounts or []}
        self._credential_setup = credential_setup
        self._credential_present = credential_present or (lambda _: False)

    def authorized(self, user_id: int | None) -> bool:
        ok = user_id is not None and user_id in self._admins
        if not ok:
            log.warning("telegram_unauthorized", extra={"telegram_user_id": user_id})
        return ok

    def status(self, user_id: int | None) -> Reply:
        """§56 system status."""
        if not self.authorized(user_id):
            return NOT_AUTHORIZED
        return Reply(self._status() if self._status else "Status unavailable.")

    def accounts(self, user_id: int | None) -> Reply:
        """List masters; passwords are accepted only by the secure setup page."""
        if not self.authorized(user_id):
            return NOT_AUTHORIZED
        if not self._master_accounts:
            return Reply("No master accounts are configured in config/accounts.yaml.")
        lines = ["MASTER ACCOUNTS", ""]
        buttons: list[list[Button]] = []
        for account in self._master_accounts.values():
            saved = self._credential_present(account.id)
            lines.append(
                f"{'CREDENTIALS SAVED' if saved else 'SETUP REQUIRED'} | "
                f"{account.id} | Method {int(account.method)}"
            )
            label = "Replace credentials" if saved else "Set credentials"
            buttons.append([(label, f"account:setup:{account.id}")])
        lines += ["", "Passwords are entered only on the private one-time setup page."]
        return Reply("\n".join(lines), buttons)

    # --- dashboard (§31) ------------------------------------------------------------------

    def dashboard(self, user_id: int | None) -> Reply:
        if not self.authorized(user_id):
            return NOT_AUTHORIZED
        gw = self._gateway
        connected = gw is not None and gw.is_connected()
        account = gw.account_snapshot() if gw is not None and connected else None
        with self._sessions.begin() as s:
            status = controls.trading_status(s)
            mode = controls.risk_mode(s)
            scopes = s.scalars(select(ProgressionStateRow)).all()
            today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            # Summed in Python: SQL SUM() over TEXT decimals would go through floats.
            closed = s.execute(
                select(Trade.result, Trade.net_profit).where(Trade.closed_at >= today)
            ).all()
        counts: dict[str | None, int] = {}
        for result, _ in closed:
            counts[result] = counts.get(result, 0) + 1
        pnl = sum((p for _, p in closed if p is not None), Decimal(0))
        status_line = {
            TradingStatus.RUNNING: "🟢 Running",
            TradingStatus.PAUSED: "⏸ Paused",
            TradingStatus.STOPPED: "⛔ Stopped",
        }[status]
        lines = [
            "🚜 PLOUGH BACKER",
            "",
            f"Status:       {status_line}",
            f"Mode:         {self._mode}",
            f"MT5:          {'🟢 Connected' if connected else '🔴 Disconnected'}",
            "",
            f"Balance:      {money(account.balance if account else None)}",
            f"Equity:       {money(account.equity if account else None)}",
            "",
            "Risk Method:",
            mode_label(mode),
            "",
        ]
        if self._account_gateways:
            lines += ["Accounts:"]
            for account_id, (account_gateway, account_mode) in self._account_gateways.items():
                try:
                    account_connected = account_gateway.is_connected()
                    snapshot = (
                        account_gateway.account_snapshot() if account_connected else None
                    )
                except BrokerError:
                    account_connected, snapshot = False, None
                marker = "ONLINE" if account_connected else "OFFLINE"
                lines.append(
                    f"{marker} {account_id} | Method {int(account_mode)} | "
                    f"Balance {money(snapshot.balance if snapshot else None)} | "
                    f"Equity {money(snapshot.equity if snapshot else None)}"
                )
            lines.append("")
        for row in scopes:
            lines.append(f"{row.scope_key}: {lot(row.theoretical_lot)}")
        lines += [
            "",
            "Today:",
            f"Trades: {sum(counts.values())}",
            f"Wins:   {counts.get(TradeOutcome.WIN, 0)}",
            f"Losses: {counts.get(TradeOutcome.LOSS, 0)}",
            f"P/L:    {money(pnl)}",
        ]
        pause = (
            ("⏸ Pause", "status:PAUSED")
            if status is TradingStatus.RUNNING
            else ("▶️ Resume", "status:RUNNING")
        )
        reply = Reply(
            "\n".join(lines),
            [
                [("⚙️ Risk Mode", "mode:menu"), ("🔒 Equity Lock", "lock:menu")],
                [pause, ("⛔ Stop", "status:STOPPED")],
            ],
        )
        if self._master_accounts:
            reply.buttons.insert(1, [("Accounts", "account:menu")])
        return reply

    # --- risk mode (§32, §33) -------------------------------------------------------------

    def mode_menu(self, user_id: int | None) -> Reply:
        if not self.authorized(user_id):
            return NOT_AUTHORIZED
        with self._sessions.begin() as s:
            current = controls.risk_mode(s)
        text = "⚙️ SELECT PLOUGH-BACK METHOD\n\n" + "\n".join(mode_label(m) for m in RiskMode)
        text += f"\n\nCurrent:\n{mode_label(current)}"
        buttons = [
            [(mode_label(m), f"mode:ask:{int(current)}:{int(m)}")]
            for m in RiskMode
            if m is not current
        ]
        return Reply(text, buttons)

    def mode_ask(self, user_id: int | None, current: int, new: int) -> Reply:
        if not self.authorized(user_id):
            return NOT_AUTHORIZED
        return Reply(
            f"Change from:\n\n{mode_label(RiskMode(current))}\n\nto:\n\n"
            f"{mode_label(RiskMode(new))}?\n\nThe new method starts from base lot.",
            [[("✅ Confirm", f"mode:confirm:{current}:{new}"), ("❌ Cancel", "cancel")]],
        )

    def mode_confirm(self, user_id: int | None, expected_current: int, new: int) -> Reply:
        if not self.authorized(user_id):
            return NOT_AUTHORIZED
        with self._sessions.begin() as s:
            if controls.risk_mode(s) != RiskMode(expected_current):
                return Reply("⚠️ Mode changed meanwhile — nothing applied. Open the menu again.")
            controls.change_risk_mode(s, RiskMode(new), actor=f"telegram:{user_id}")
        return Reply(f"✅ Risk method is now {mode_label(RiskMode(new))}.")

    # --- Equity Lock (§39) ----------------------------------------------------------------

    def lock_menu(self, user_id: int | None) -> Reply:
        if not self.authorized(user_id):
            return NOT_AUTHORIZED
        with self._sessions.begin() as s:
            enabled, value = controls.equity_lock(s)
        text = (
            f"🔒 EQUITY LOCK\n\nStatus: {'ENABLED' if enabled else 'DISABLED'}\n"
            f"Protected Equity: {money(value)}\n\nChange amount: /lock <amount>"
        )
        buttons = [[("Disable", "lock:ask_disable")]] if enabled else []
        return Reply(text, buttons)

    def lock_ask(self, user_id: int | None, amount_text: str) -> Reply:
        if not self.authorized(user_id):
            return NOT_AUTHORIZED
        try:
            amount = Decimal(amount_text.strip().lstrip("$").replace(",", ""))
        except InvalidOperation:
            return Reply("Usage: /lock 500.00")
        if not amount.is_finite() or amount < 0:
            return Reply("Amount must be a non-negative number.")
        return Reply(
            f"Enable Equity Lock at {money(amount)}?\n\n"
            "Trades whose SL loss would take equity below this are blocked.",
            [[("✅ Confirm", f"lock:confirm:{amount}"), ("❌ Cancel", "cancel")]],
        )

    def lock_confirm(self, user_id: int | None, amount: str | None) -> Reply:
        if not self.authorized(user_id):
            return NOT_AUTHORIZED
        value = None if amount is None else Decimal(amount)
        with self._sessions.begin() as s:
            controls.set_equity_lock(
                s, enabled=value is not None, value=value, actor=f"telegram:{user_id}"
            )
        return Reply(
            "🔓 Equity Lock disabled."
            if value is None
            else f"🔒 Equity Lock enabled at {money(value)}."
        )

    # --- pause / resume / stop (§34, §35) -------------------------------------------------

    def set_status(self, user_id: int | None, status: TradingStatus) -> Reply:
        if not self.authorized(user_id):
            return NOT_AUTHORIZED
        with self._sessions.begin() as s:
            controls.set_trading_status(s, status, actor=f"telegram:{user_id}")
        return Reply(
            {
                TradingStatus.PAUSED: "⏸ Paused. Signals are still recorded; no new MT5 orders.",
                TradingStatus.RUNNING: "▶️ Resumed.",
                TradingStatus.STOPPED: "⛔ Kill switch active. No new trades.\n"
                "Existing MT5 positions are NOT closed.",
            }[status]
        )

    # --- callback router ------------------------------------------------------------------

    def callback(self, user_id: int | None, data: str) -> Reply:
        parts = data.split(":")
        match parts:
            case ["account", "menu"]:
                return self.accounts(user_id)
            case ["mode", "menu"]:
                return self.mode_menu(user_id)
            case ["mode", "ask", cur, new]:
                return self.mode_ask(user_id, int(cur), int(new))
            case ["mode", "confirm", cur, new]:
                return self.mode_confirm(user_id, int(cur), int(new))
            case ["lock", "menu"]:
                return self.lock_menu(user_id)
            case ["lock", "ask_disable"]:
                if not self.authorized(user_id):
                    return NOT_AUTHORIZED
                return Reply(
                    "Disable Equity Lock?",
                    [[("✅ Confirm", "lock:confirm_disable"), ("❌ Cancel", "cancel")]],
                )
            case ["lock", "confirm", amount]:
                return self.lock_confirm(user_id, amount)
            case ["lock", "confirm_disable"]:
                return self.lock_confirm(user_id, None)
            case ["status", status]:
                return self.set_status(user_id, TradingStatus(status))
            case ["account", "setup", account_id]:
                if not self.authorized(user_id):
                    return NOT_AUTHORIZED
                if account_id not in self._master_accounts or self._credential_setup is None:
                    return Reply("Account credential setup is unavailable.")
                assert user_id is not None
                link = self._credential_setup.issue(account_id, user_id)
                return Reply(
                    "Open this private link within 10 minutes. It works once.\n\n"
                    f"{link}\n\nNever send an MT5 password in Telegram."
                )
            case ["cancel"]:
                return Reply("Cancelled.") if self.authorized(user_id) else NOT_AUTHORIZED
        return Reply("Unknown action.") if self.authorized(user_id) else NOT_AUTHORIZED
