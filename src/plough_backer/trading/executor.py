"""Execution orchestrator (README §7, §28, Phase 6).

Normalized signal -> progression -> broker-limit safety reset -> volume normalization ->
risk -> Equity Lock -> MT5 -> persistence. Outcomes change progression only at settlement;
the safety reset is persisted before an at/above-max lot can be traded.

Crash safety (§27, INV-15), three transactions:
  1. ingest + parse + dedupe + size + claim (journal row with unique fingerprint) — COMMIT
  2. order_send — outside any DB transaction
  3. record broker response
A crash after 1 leaves a claimed row: restart can never submit that fingerprint again;
reconciliation (Phase 8) resolves what MT5 actually did.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from plough_backer import controls
from plough_backer.config import SourcesConfig, SymbolsConfig
from plough_backer.enums import (
    AuditEventType,
    Direction,
    ExecutionMode,
    OrderType,
    ProgressionScope,
    RejectionReason,
    RiskEntrySource,
    SignalState,
    TradingStatus,
    VolumeMaxPolicy,
)
from plough_backer.exceptions import (
    BrokerError,
    BrokerRejected,
    DuplicateSignal,
    ExecutionFailed,
    InvalidSignal,
    MT5Unavailable,
    ParseRejected,
)
from plough_backer.persistence import repositories as repo
from plough_backer.persistence.models import Signal, Trade
from plough_backer.risk.engine import RiskAssessment, assess_risk
from plough_backer.risk.equity_lock import EquityLockDecision, check_equity_lock
from plough_backer.risk.normalization import normalize_volume
from plough_backer.risk.progression import ProgressionState, reset_at_broker_limit
from plough_backer.signals.fingerprint import fingerprint
from plough_backer.signals.models import NormalizedSignal
from plough_backer.signals.parser import get_parser
from plough_backer.trading.gateway import BrokerGateway, OrderRequest, OrderResult

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionPolicy:
    """Choices the README leaves open. Deliberately no defaults (§76)."""

    execution_mode: ExecutionMode
    progression_scope: ProgressionScope
    volume_max_policy: VolumeMaxPolicy  # Q-05
    risk_entry: RiskEntrySource  # Q-06


@dataclass(slots=True)
class Outcome:
    """What happened to one message — the input for Telegram notifications."""

    state: SignalState
    signal_id: str | None = None
    source_id: str = ""
    reason: str | None = None
    signal: NormalizedSignal | None = None
    theoretical_lot: Decimal | None = None
    executed_lot: Decimal | None = None
    volume_capped: bool = False
    risk: RiskAssessment | None = None
    lock: EquityLockDecision | None = None
    result: OrderResult | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EditOutcome:
    signal_id: str
    executed: bool
    mt5_ticket: int | None


def _broker_state(exc: BrokerError) -> SignalState:
    """Broker said no (constraint/retcode) vs the request could not complete (§21)."""
    if isinstance(exc, ExecutionFailed | MT5Unavailable):
        return SignalState.EXECUTION_FAILED
    return SignalState.BROKER_REJECTED


def scope_key(scope: ProgressionScope, source_id: str, symbol: str) -> str:
    # ponytail: Q-01 provisional option (a): one independent progression per symbol within
    # the scope, based on that symbol's own volume_min. Option (b) would change this key.
    owner = source_id if scope is ProgressionScope.PER_SOURCE else "*"
    return f"{scope}:{owner}:{symbol}"


class Orchestrator:
    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        gateway: BrokerGateway | None,
        symbols: SymbolsConfig,
        sources: SourcesConfig,
        policy: ExecutionPolicy,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._gateway = gateway
        self._symbols = symbols
        self._sources = {s.id: s for s in sources.sources}
        self._policy = policy
        self._now = clock
        # Serializes the whole read-size-claim step (§26); DB BEGIN IMMEDIATE is the 2nd guard.
        self._lock = threading.Lock()

    def process_message(
        self,
        *,
        source_id: str,
        message_id: int,
        message_time: datetime,
        text: str,
        received_at: datetime,
    ) -> Outcome:
        with self._lock:
            outcome, request, trade_id = self._prepare(
                source_id, message_id, message_time, text, received_at
            )
        if request is None or trade_id is None:
            return outcome
        return self._submit(outcome, request, trade_id)

    def process_edit(
        self, *, source_id: str, message_id: int, text: str, edited_at: datetime
    ) -> EditOutcome | None:
        """§25: store the edit; NEVER modify or (re)execute anything automatically.

        Returns None if the original message was never ingested. Edits of messages that
        did not execute are recorded only (Q-13 decides whether they may be re-parsed).
        """
        with self._sessions.begin() as s:
            signal = s.scalar(
                select(Signal).where(
                    Signal.source_id == source_id, Signal.source_message_id == message_id
                )
            )
            if signal is None:
                return None
            signal.edited_message, signal.edited_at = text, edited_at
            trade = s.scalar(select(Trade).where(Trade.signal_pk == signal.id))
            ticket = trade.mt5_order_id if trade is not None else None
            repo.append_audit(
                s,
                AuditEventType.SIGNAL_RECEIVED,
                signal_id=signal.signal_id,
                edited=True,
                executed=trade is not None,
            )
            return EditOutcome(signal.signal_id, executed=trade is not None, mt5_ticket=ticket)

    # --- transaction 1 --------------------------------------------------------------------

    def _prepare(
        self,
        source_id: str,
        message_id: int,
        message_time: datetime,
        text: str,
        received_at: datetime,
    ) -> tuple[Outcome, OrderRequest | None, int | None]:
        with self._sessions.begin() as s:
            signal = repo.ingest_message(
                s,
                source_id=source_id,
                source_message_id=message_id,
                source_timestamp=message_time,
                received_at=received_at,
                raw_message=text,
            )
            if signal is None:  # same Telegram message seen again (restart, catch-up)
                repo.append_audit(
                    s,
                    AuditEventType.SIGNAL_DUPLICATE,
                    source_id=source_id,
                    source_message_id=message_id,
                    reason="MESSAGE_ALREADY_INGESTED",
                )  # evidence for §68 #17 and the §49 duplicate count
                return (
                    Outcome(
                        SignalState.REJECTED_DUPLICATE,
                        source_id=source_id,
                        reason="MESSAGE_ALREADY_INGESTED",
                    ),
                    None,
                    None,
                )
            repo.append_audit(s, AuditEventType.SIGNAL_RECEIVED, signal_id=signal.signal_id)
            out = Outcome(SignalState.RECEIVED, signal.signal_id, source_id)

            source = self._sources.get(source_id)
            if source is None or not source.enabled:
                return (
                    self._end(
                        s,
                        signal,
                        out,
                        SignalState.REJECTED_INVALID_SIGNAL,
                        RejectionReason.UNKNOWN_SOURCE,
                    ),
                    None,
                    None,
                )
            repo.transition_signal(s, signal, SignalState.SOURCE_VALIDATED)

            try:
                parsed = get_parser(source.parser_profile)(
                    text,
                    symbols=self._symbols,
                    signal_id=signal.signal_id,
                    source_id=source_id,
                    source_message_id=message_id,
                    source_timestamp=message_time,
                )
            except ParseRejected as exc:
                return (
                    self._end(s, signal, out, SignalState.REJECTED_PARSE, exc.reason, exc.detail),
                    None,
                    None,
                )
            except InvalidSignal as exc:
                return (
                    self._end(
                        s, signal, out, SignalState.REJECTED_INVALID_SIGNAL, exc.reason, exc.detail
                    ),
                    None,
                    None,
                )

            ns = out.signal = parsed.signal
            fp = fingerprint(ns)
            self._store_parsed(signal, ns, fp)
            for state in (
                SignalState.PARSED,
                SignalState.NORMALIZED,
                SignalState.SYMBOL_RESOLVED,
                SignalState.VALIDATED,
            ):
                repo.transition_signal(s, signal, state)
            repo.append_audit(s, AuditEventType.SIGNAL_PARSED, signal_id=signal.signal_id)

            if repo.fingerprint_executed(s, fp):
                repo.append_audit(s, AuditEventType.SIGNAL_DUPLICATE, signal_id=signal.signal_id)
                return (
                    self._end(
                        s,
                        signal,
                        out,
                        SignalState.REJECTED_DUPLICATE,
                        "FINGERPRINT_ALREADY_EXECUTED",
                    ),
                    None,
                    None,
                )
            repo.transition_signal(s, signal, SignalState.DEDUPLICATED)

            # §34/§35: parsed + journaled, but no new MT5 execution; progression untouched.
            status = controls.trading_status(s)
            if status is TradingStatus.PAUSED:
                return self._end(s, signal, out, SignalState.SKIPPED_PAUSED), None, None
            if status is TradingStatus.STOPPED:
                return self._end(s, signal, out, SignalState.SKIPPED_STOPPED), None, None
            gw = self._gateway
            if gw is None or not gw.is_connected():
                # §57: record, never replay automatically after reconnect.
                return self._end(s, signal, out, SignalState.SKIPPED_MT5_UNAVAILABLE), None, None

            try:
                return self._size_and_claim(
                    s, gw, signal, ns, fp, parsed.labelled_take_profits, out, received_at
                )
            except BrokerError as exc:  # broker constraint hit before sending (§21)
                return (
                    self._end(s, signal, out, _broker_state(exc), type(exc).__name__, str(exc)),
                    None,
                    None,
                )

    def _size_and_claim(
        self,
        s: Session,
        gw: BrokerGateway,
        signal: Signal,
        ns: NormalizedSignal,
        fp: str,
        tps: dict[int, Decimal],
        out: Outcome,
        received_at: datetime,
    ) -> tuple[Outcome, OrderRequest | None, int | None]:
        assert ns.symbol_mt5 is not None
        spec = gw.symbol_specification(ns.symbol_mt5)  # §28 steps 2-4
        account = gw.account_snapshot()
        mode = controls.risk_mode(s)

        key = scope_key(self._policy.progression_scope, ns.source_id, ns.symbol_mt5)
        loaded = repo.load_progression(s, key)
        if loaded is None or loaded[0].mode is not mode:
            state = ProgressionState.initial(mode, spec.volume_min)  # §14 base = volume_min
            if loaded is None:
                version = repo.create_progression(s, key, state)
            else:  # mode changed since this scope last traded (§33)
                version = repo.save_progression(s, key, state, expected_version=loaded[1])
        else:
            state, version = loaded

        previous_state = state
        state, progression_reset = reset_at_broker_limit(
            state, volume_min=spec.volume_min, volume_max=spec.volume_max
        )
        if progression_reset:
            version = repo.save_progression(s, key, state, expected_version=version)
            repo.append_audit(
                s,
                AuditEventType.PROGRESSION_RESET,
                scope_key=key,
                symbol=ns.symbol_mt5,
                old_theoretical_lot=previous_state.theoretical_lot,
                new_theoretical_lot=state.theoretical_lot,
                broker_volume_max=spec.volume_max,
            )
            log.warning(
                "progression_reset_at_broker_limit",
                extra={
                    "scope_key": key,
                    "symbol": ns.symbol_mt5,
                    "old_theoretical_lot": previous_state.theoretical_lot,
                    "new_theoretical_lot": state.theoretical_lot,
                    "broker_volume_max": spec.volume_max,
                },
            )
        out.theoretical_lot = state.theoretical_lot

        volume = normalize_volume(
            state.theoretical_lot,
            spec.volume_min,
            spec.volume_max,
            spec.volume_step,
            max_policy=self._policy.volume_max_policy,
        )
        out.executed_lot, out.volume_capped = volume.executable, volume.capped_at_max
        if volume.capped_at_max:
            log.warning(
                "volume_capped",
                extra={
                    "signal_id": signal.signal_id,
                    "theoretical_lot": volume.theoretical,
                    "executed_lot": volume.executable,
                },
            )
        repo.transition_signal(s, signal, SignalState.SIZED)

        entry = self._risk_entry(gw, ns)
        risk = out.risk = assess_risk(
            gw,
            symbol=ns.symbol_mt5,
            direction=ns.direction,
            executed_lot=volume.executable,
            entry_price=entry,
            stop_loss=ns.stop_loss,
            take_profit=ns.selected_take_profit,
            equity=account.equity,
        )

        lock_enabled, lock_value = controls.equity_lock(s)
        lock = out.lock = check_equity_lock(
            enabled=lock_enabled,
            protected_equity=lock_value,
            current_equity=account.equity,
            risk_to_sl=risk.risk_to_sl,
        )
        if not lock.allowed:
            return self._end(s, signal, out, SignalState.BLOCKED_EQUITY_LOCK), None, None
        repo.transition_signal(s, signal, SignalState.EQUITY_LOCK_CHECKED)

        if self._policy.execution_mode is ExecutionMode.PAPER:
            # Q-09: no settlement source for paper trades yet — sizing is journaled only.
            return self._end(s, signal, out, SignalState.SKIPPED_PAPER), None, None

        now = self._now()
        trade = Trade(
            signal_pk=signal.id,
            signal_id=signal.signal_id,
            signal_fingerprint=fp,
            progression_scope_key=key,
            telegram_source_id=ns.source_id,
            telegram_message_id=ns.source_message_id,
            telegram_timestamp=ns.source_timestamp,
            raw_signal=ns.raw_message,
            parser_version=ns.parser_version,
            symbol_raw=ns.symbol_raw,
            symbol_mt5=ns.symbol_mt5,
            direction=ns.direction,
            order_type=ns.order_type,
            requested_entry=ns.entry,
            stop_loss=ns.stop_loss,
            tp1=tps.get(1),
            tp2=tps.get(2),
            tp3=tps.get(3),
            selected_tp=ns.selected_take_profit,
            risk_mode=int(mode),
            base_lot=state.base_lot,
            theoretical_lot=state.theoretical_lot,
            executed_lot=volume.executable,
            volume_min=spec.volume_min,
            volume_max=spec.volume_max,
            volume_step=spec.volume_step,
            volume_capped_at_max=volume.capped_at_max,
            estimated_risk=risk.risk_to_sl,
            estimated_reward=risk.reward_to_tp,
            estimated_rr=risk.reward_risk_ratio,
            equity_risk_percent=risk.equity_risk_percent,
            balance_before=account.balance,
            equity_before=account.equity,
            margin_before=account.margin,
            free_margin_before=account.free_margin,
            equity_lock_enabled=lock_enabled,
            equity_lock_value=lock_value,
            telegram_received_at=received_at,
            parsed_at=now,
            execution_requested_at=now,
        )
        try:
            repo.claim_execution(s, trade)
        except DuplicateSignal:
            return (
                self._end(
                    s, signal, out, SignalState.REJECTED_DUPLICATE, "FINGERPRINT_ALREADY_EXECUTED"
                ),
                None,
                None,
            )
        repo.transition_signal(s, signal, SignalState.EXECUTION_REQUESTED)
        repo.append_audit(
            s,
            AuditEventType.TRADE_REQUESTED,
            signal_id=signal.signal_id,
            executed_lot=volume.executable,
        )
        out.state = SignalState.EXECUTION_REQUESTED
        request = OrderRequest(
            symbol=ns.symbol_mt5,
            direction=ns.direction,
            order_type=ns.order_type,
            volume=volume.executable,
            entry=ns.entry,
            stop_loss=ns.stop_loss,
            take_profit=ns.selected_take_profit,
            comment=signal.signal_id,
        )
        return out, request, trade.id

    def _risk_entry(self, gw: BrokerGateway, ns: NormalizedSignal) -> Decimal:
        if ns.order_type is not OrderType.MARKET:
            assert ns.entry is not None
            return ns.entry  # pending orders fill at their own price
        if self._policy.risk_entry is RiskEntrySource.SIGNAL_ENTRY_OR_LIVE and ns.entry:
            return ns.entry
        assert ns.symbol_mt5 is not None
        tick = gw.current_tick(ns.symbol_mt5)
        return tick.ask if ns.direction is Direction.BUY else tick.bid

    # --- transactions 2 + 3 ---------------------------------------------------------------

    def _submit(self, out: Outcome, request: OrderRequest, trade_id: int) -> Outcome:
        assert self._gateway is not None
        try:
            result = self._gateway.submit_order(request)
        except BrokerError as exc:
            state = _broker_state(exc)
            retcode = exc.retcode if isinstance(exc, BrokerRejected) else None
            with self._sessions.begin() as s:
                trade = s.get(Trade, trade_id)
                assert trade is not None
                trade.execution_retcode, trade.execution_message = retcode, str(exc)
                trade.broker_response_at = self._now()
                signal = s.get(Signal, trade.signal_pk)
                assert signal is not None
                repo.append_audit(
                    s, AuditEventType.TRADE_REJECTED, signal_id=trade.signal_id, error=str(exc)
                )
                return self._end(s, signal, out, state, type(exc).__name__, str(exc))

        out.result = result
        with self._sessions.begin() as s:
            trade = s.get(Trade, trade_id)
            assert trade is not None
            trade.mt5_order_id, trade.mt5_deal_id = result.order_id, result.deal_id
            trade.execution_retcode, trade.execution_message = result.retcode, result.message
            trade.execution_requested_at = result.request_sent_at
            trade.broker_response_at = result.response_at
            trade.executed_lot = result.volume  # actual fill — never pretend (§17)
            trade.actual_entry = result.price
            signal = s.get(Signal, trade.signal_pk)
            assert signal is not None
            repo.transition_signal(s, signal, SignalState.EXECUTED)
            if not request.order_type.is_pending:
                # MT5: a position's id is the ticket of the order that opened it.
                trade.mt5_position_id = result.order_id
                trade.opened_at = trade.position_confirmed_at = result.response_at
                repo.transition_signal(s, signal, SignalState.OPEN)
            repo.append_audit(
                s,
                AuditEventType.TRADE_EXECUTED,
                signal_id=trade.signal_id,
                mt5_order_id=result.order_id,
                executed_lot=result.volume,
            )
            out.state = signal.state  # type: ignore[assignment]
            out.executed_lot = result.volume
        return out

    # --- helpers --------------------------------------------------------------------------

    @staticmethod
    def _store_parsed(signal: Signal, ns: NormalizedSignal, fp: str) -> None:
        signal.parser_version, signal.fingerprint = ns.parser_version, fp
        signal.parsed_at = datetime.now(UTC)
        signal.symbol_raw, signal.symbol_mt5 = ns.symbol_raw, ns.symbol_mt5
        signal.direction, signal.order_type = ns.direction, ns.order_type
        signal.entry, signal.stop_loss = ns.entry, ns.stop_loss
        signal.take_profits = [format(t, "f") for t in ns.take_profits]
        signal.selected_take_profit = ns.selected_take_profit

    @staticmethod
    def _end(
        s: Session,
        signal: Signal,
        out: Outcome,
        state: SignalState,
        reason: str | None = None,
        detail: str | None = None,
    ) -> Outcome:
        repo.transition_signal(s, signal, state, reason)
        if state.name.startswith(("REJECTED", "BLOCKED")):
            repo.append_audit(
                s,
                AuditEventType.SIGNAL_REJECTED,
                signal_id=signal.signal_id,
                state=state,
                reason=reason,
                detail=detail,
            )
        out.state, out.reason = state, reason
        if detail:
            out.extra["detail"] = detail
        return out
