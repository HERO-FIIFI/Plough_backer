"""ORM models (README §42, §43, §51, §52). Schema changes only via Alembic migrations.

Uniqueness constraints carry the idempotency guarantees (INV-01, INV-15, §27):
- signals(source_id, source_message_id): a Telegram message is ingested once;
- trades.signal_fingerprint: a fingerprint executes at most once, across restarts;
- trades.mt5_position_id: a broker position maps to one journal row (settlement once, §65).
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, ForeignKey, MetaData, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from plough_backer.persistence.database import DecimalText, UTCDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    # Named constraints so SQLite batch migrations can alter them later.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    # Every Mapped[Decimal] is exact TEXT and every Mapped[datetime] is tz-aware UTC.
    type_annotation_map = {  # noqa: RUF012 — SQLAlchemy's API
        Decimal: DecimalText(),
        datetime: UTCDateTime(),
        dict[str, Any]: JSON(),
        list[str]: JSON(),
    }


class Setting(Base):
    """Runtime settings changed from Telegram (risk mode, Equity Lock, pause…). Q-17."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str | None]


class Signal(Base):
    """Every received Telegram message, valid or not (§7, §10 "message recorded")."""

    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("source_id", "source_message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[str] = mapped_column(unique=True)  # "PB-000184"
    source_id: Mapped[str] = mapped_column(index=True)
    source_message_id: Mapped[int]
    source_timestamp: Mapped[datetime]
    received_at: Mapped[datetime]
    raw_message: Mapped[str]
    edited_message: Mapped[str | None]  # §25: keep both original and edited text
    edited_at: Mapped[datetime | None]

    parser_version: Mapped[str | None]
    parsed_at: Mapped[datetime | None]
    fingerprint: Mapped[str | None] = mapped_column(index=True)
    symbol_raw: Mapped[str | None]
    symbol_mt5: Mapped[str | None]
    direction: Mapped[str | None]
    order_type: Mapped[str | None]
    entry: Mapped[Decimal | None]
    stop_loss: Mapped[Decimal | None]
    take_profits: Mapped[list[str] | None]  # Decimal strings, in signal order
    selected_take_profit: Mapped[Decimal | None]

    state: Mapped[str] = mapped_column(index=True)  # enums.SignalState
    rejection_reason: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SignalEvent(Base):
    """Persisted lifecycle transitions (§7). Append-only (DB trigger)."""

    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_pk: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    from_state: Mapped[str | None]
    to_state: Mapped[str]
    reason: Mapped[str | None]
    at: Mapped[datetime] = mapped_column(default=utcnow)


class Trade(Base):
    """Trade journal (§42) + latency timestamps (§50). Created at EXECUTION_REQUESTED."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_pk: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    signal_id: Mapped[str]
    signal_fingerprint: Mapped[str] = mapped_column(unique=True)
    progression_scope_key: Mapped[str] = mapped_column(index=True)

    telegram_source_id: Mapped[str]
    telegram_message_id: Mapped[int]
    telegram_timestamp: Mapped[datetime]
    raw_signal: Mapped[str]
    parser_version: Mapped[str]

    symbol_raw: Mapped[str]
    symbol_mt5: Mapped[str] = mapped_column(index=True)
    direction: Mapped[str]
    order_type: Mapped[str]

    requested_entry: Mapped[Decimal | None]
    actual_entry: Mapped[Decimal | None]
    stop_loss: Mapped[Decimal]
    tp1: Mapped[Decimal | None]
    tp2: Mapped[Decimal | None]
    tp3: Mapped[Decimal | None]
    selected_tp: Mapped[Decimal]

    risk_mode: Mapped[int]
    base_lot: Mapped[Decimal]
    theoretical_lot: Mapped[Decimal]
    executed_lot: Mapped[Decimal]
    volume_min: Mapped[Decimal]
    volume_max: Mapped[Decimal]
    volume_step: Mapped[Decimal]
    volume_capped_at_max: Mapped[bool] = mapped_column(default=False)

    estimated_risk: Mapped[Decimal]
    estimated_reward: Mapped[Decimal]
    estimated_rr: Mapped[Decimal | None]
    equity_risk_percent: Mapped[Decimal | None]

    balance_before: Mapped[Decimal]
    equity_before: Mapped[Decimal]
    margin_before: Mapped[Decimal]
    free_margin_before: Mapped[Decimal]
    equity_lock_enabled: Mapped[bool]
    equity_lock_value: Mapped[Decimal | None]

    mt5_order_id: Mapped[int | None]
    mt5_deal_id: Mapped[int | None]
    mt5_position_id: Mapped[int | None] = mapped_column(unique=True)
    execution_retcode: Mapped[int | None]
    execution_message: Mapped[str | None]

    opened_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    close_price: Mapped[Decimal | None]
    realized_profit: Mapped[Decimal | None]
    realized_swap: Mapped[Decimal | None]
    realized_commission: Mapped[Decimal | None]
    net_profit: Mapped[Decimal | None]
    result: Mapped[str | None]  # enums.TradeOutcome
    realized_r_multiple: Mapped[Decimal | None]
    needs_review: Mapped[bool] = mapped_column(default=False)  # §22 OTHER
    settled_at: Mapped[datetime | None]  # set exactly once, with the progression update
    balance_after: Mapped[Decimal | None]
    equity_after: Mapped[Decimal | None]

    telegram_received_at: Mapped[datetime]
    parsed_at: Mapped[datetime | None]
    execution_requested_at: Mapped[datetime | None]
    broker_response_at: Mapped[datetime | None]
    position_confirmed_at: Mapped[datetime | None]

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ProgressionStateRow(Base):
    """§52. scope_key is opaque (composition decided by Q-01). version = optimistic lock."""

    __tablename__ = "progression_states"

    scope_key: Mapped[str] = mapped_column(primary_key=True)
    mode: Mapped[int]
    base_lot: Mapped[Decimal]
    theoretical_lot: Mapped[Decimal]
    wins: Mapped[int]
    losses: Mapped[int]
    mode5_block_trade_count: Mapped[int]
    version: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AuditEvent(Base):
    """§43. Append-only (DB trigger)."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(index=True)  # enums.AuditEventType
    actor: Mapped[str | None]  # e.g. Telegram user id for MODE_CHANGED (§33)
    payload: Mapped[dict[str, Any]]
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
