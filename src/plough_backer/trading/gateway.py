"""Broker boundary. Domain code (risk, shadow) depends on this, never on MetaTrader5.

Implementations: the real MT5 adapter (trading/mt5_client.py, Phase 5) and in-memory
fakes for PAPER mode and tests. Order submission/history methods are added in Phase 5
together with their request/result types.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from plough_backer.enums import Direction, OrderType


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolSpecification:
    """MT5 symbol fields named in README §13. Always fetched from the broker, never assumed.

    Persist the spec used for each trade (§14: base lot must be stored per trade).
    """

    name: str
    volume_min: Decimal  # == base lot (§14)
    volume_max: Decimal
    volume_step: Decimal
    trade_tick_size: Decimal
    trade_tick_value: Decimal
    trade_contract_size: Decimal
    digits: int
    point: Decimal
    trade_stops_level: int
    trade_mode: int  # raw MT5 SYMBOL_TRADE_MODE_* value

    @property
    def base_lot(self) -> Decimal:
        return self.volume_min


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountSnapshot:
    """Account state captured before a trade (README §42 *_before fields)."""

    balance: Decimal
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class Tick:
    bid: Decimal
    ask: Decimal
    time: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderRequest:
    symbol: str
    direction: Direction
    order_type: OrderType
    volume: Decimal  # executable (normalized) lot — never the theoretical one
    entry: Decimal | None  # required for pending orders; ignored for MARKET
    stop_loss: Decimal
    take_profit: Decimal
    comment: str  # signal id, e.g. "PB-000184"


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderResult:
    """Only built for broker-confirmed success (§28: retcode validated, not just returned)."""

    retcode: int
    message: str
    order_id: int
    deal_id: int | None  # None for a placed pending order
    price: Decimal | None
    volume: Decimal  # what the broker actually filled — may differ on partial fill
    partial_fill: bool
    request_sent_at: datetime
    response_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class Deal:
    """One MT5 history deal (§29: history is authoritative for realized P/L, INV-14)."""

    ticket: int
    order_id: int
    position_id: int
    entry: int  # raw MT5 DEAL_ENTRY_* (0 in, 1 out, 2 inout, 3 out_by)
    reason: int  # raw MT5 DEAL_REASON_* (SL/TP/client/…) — classification is Phase 8
    volume: Decimal
    price: Decimal
    profit: Decimal
    swap: Decimal
    commission: Decimal
    fee: Decimal
    time: datetime
    magic: int
    comment: str


class BrokerGateway(Protocol):
    def is_connected(self) -> bool: ...

    def account_snapshot(self) -> AccountSnapshot: ...

    def symbol_specification(self, symbol: str) -> SymbolSpecification: ...

    def calc_profit(
        self,
        symbol: str,
        direction: Direction,
        volume: Decimal,
        price_open: Decimal,
        price_close: Decimal,
    ) -> Decimal:
        """Broker-native P/L for a hypothetical trade (README §18: prefer MT5 calculation)."""
        ...

    def current_tick(self, symbol: str) -> Tick: ...

    def submit_order(self, request: OrderRequest) -> OrderResult:
        """Raises BrokerRejected / ExecutionFailed / MT5Unavailable instead of failing quietly."""
        ...

    def open_position_ids(self) -> set[int]: ...

    def deals_for_position(self, position_id: int) -> list[Deal]: ...
