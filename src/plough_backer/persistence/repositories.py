"""Repository functions. Callers own the transaction: `with sessions.begin() as s: ...`.

Every transaction starts with BEGIN IMMEDIATE (database.py), so read-then-write sequences
in one transaction are serialized. Progression rows also carry an optimistic `version`
as a second guard (§26, §52).
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from plough_backer.enums import AuditEventType, RiskMode, SignalState
from plough_backer.exceptions import ConcurrencyConflict, DuplicateSignal
from plough_backer.persistence.models import (
    AuditEvent,
    ProgressionStateRow,
    Setting,
    Signal,
    SignalEvent,
    Trade,
)
from plough_backer.risk.progression import ProgressionState


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    """Decimals as exact strings, datetimes as ISO — never floats in stored JSON."""

    def default(v: Any) -> str:
        if isinstance(v, Decimal):
            return format(v, "f")
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v)

    return cast(dict[str, Any], json.loads(json.dumps(payload, default=default)))


# --- Progression (§52) --------------------------------------------------------------------


def load_progression(session: Session, scope_key: str) -> tuple[ProgressionState, int] | None:
    row = session.get(ProgressionStateRow, scope_key)
    if row is None:
        return None
    state = ProgressionState(
        mode=RiskMode(row.mode),
        base_lot=row.base_lot,
        theoretical_lot=row.theoretical_lot,
        wins=row.wins,
        losses=row.losses,
        mode5_block_trade_count=row.mode5_block_trade_count,
    )
    return state, row.version


def _columns(state: ProgressionState) -> dict[str, Any]:
    return {
        "mode": int(state.mode),
        "base_lot": state.base_lot,
        "theoretical_lot": state.theoretical_lot,
        "wins": state.wins,
        "losses": state.losses,
        "mode5_block_trade_count": state.mode5_block_trade_count,
    }


def create_progression(
    session: Session,
    scope_key: str,
    state: ProgressionState,
    *,
    account_id: str = "default",
) -> int:
    session.add(
        ProgressionStateRow(
            scope_key=scope_key, account_id=account_id, version=1, **_columns(state)
        )
    )
    session.flush()
    return 1


def save_progression(
    session: Session, scope_key: str, state: ProgressionState, *, expected_version: int
) -> int:
    """Write `state` only if nobody else changed the row since it was read."""
    stmt = (
        update(ProgressionStateRow)
        .where(
            ProgressionStateRow.scope_key == scope_key,
            ProgressionStateRow.version == expected_version,
        )
        .values(**_columns(state), version=expected_version + 1, updated_at=datetime.now(UTC))
    )
    result = cast(CursorResult[Any], session.execute(stmt))
    if result.rowcount != 1:
        raise ConcurrencyConflict(f"progression {scope_key!r} is not at version {expected_version}")
    return expected_version + 1


# --- Signals (§7, §27) --------------------------------------------------------------------


def ingest_message(
    session: Session,
    *,
    source_id: str,
    source_message_id: int,
    source_timestamp: datetime,
    received_at: datetime,
    raw_message: str,
) -> Signal | None:
    """Record a Telegram message once. Returns None if it was already ingested (idempotent)."""
    exists = session.scalar(
        select(Signal.id).where(
            Signal.source_id == source_id, Signal.source_message_id == source_message_id
        )
    )
    if exists is not None:
        return None
    next_number = (session.scalar(select(func.max(Signal.id))) or 0) + 1
    signal = Signal(
        signal_id=f"PB-{next_number:06d}",
        source_id=source_id,
        source_message_id=source_message_id,
        source_timestamp=source_timestamp,
        received_at=received_at,
        raw_message=raw_message,
        state=SignalState.RECEIVED,
    )
    session.add(signal)
    session.flush()
    session.add(SignalEvent(signal_pk=signal.id, from_state=None, to_state=SignalState.RECEIVED))
    return signal


def transition_signal(
    session: Session, signal: Signal, to_state: SignalState, reason: str | None = None
) -> None:
    """Persist a lifecycle transition (§7). The allowed-transition graph awaits Q-11."""
    session.add(
        SignalEvent(signal_pk=signal.id, from_state=signal.state, to_state=to_state, reason=reason)
    )
    signal.state = to_state
    if reason is not None:
        signal.rejection_reason = reason
    session.flush()


# --- Executions (INV-01, INV-15) ----------------------------------------------------------


def fingerprint_executed(
    session: Session, fingerprint: str, *, account_id: str = "default"
) -> bool:
    found = session.scalar(
        select(Trade.id).where(
            Trade.account_id == account_id, Trade.signal_fingerprint == fingerprint
        )
    )
    return found is not None


def claim_execution(session: Session, trade: Trade) -> None:
    """Insert the journal row BEFORE sending to MT5. The unique fingerprint makes a second
    claim fail, so a signal can never be submitted twice — even after a crash/restart."""
    try:
        with session.begin_nested():
            session.add(trade)
            session.flush()
    except IntegrityError as exc:
        raise DuplicateSignal(trade.signal_fingerprint) from exc


# --- Audit (§43) --------------------------------------------------------------------------


def append_audit(
    session: Session,
    event_type: AuditEventType,
    *,
    actor: str | None = None,
    account_id: str | None = None,
    **payload: Any,
) -> None:
    session.add(
        AuditEvent(
            event_type=event_type,
            actor=actor,
            account_id=account_id,
            payload=_jsonable(payload),
        )
    )


# --- Settings (Q-17: env seeds an empty DB; persisted values win afterwards) ---------------


def get_setting(session: Session, key: str) -> dict[str, Any] | None:
    row = session.get(Setting, key)
    return None if row is None else row.value


def seed_setting(session: Session, key: str, value: dict[str, Any]) -> bool:
    """Insert only if absent. Returns True if seeded. Never overwrites a persisted value."""
    if session.get(Setting, key) is not None:
        return False
    session.add(Setting(key=key, value=_jsonable(value), updated_by="env-seed"))
    session.flush()
    return True


def set_setting(session: Session, key: str, value: dict[str, Any], *, updated_by: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=_jsonable(value), updated_by=updated_by))
    else:
        row.value = _jsonable(value)
        row.updated_by = updated_by
    session.flush()
