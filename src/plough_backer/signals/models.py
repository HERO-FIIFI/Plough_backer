"""NormalizedSignal (README §8). Execution only ever starts from this, never raw text."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from plough_backer.enums import Direction, OrderType


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedSignal:
    signal_id: str
    source_id: str
    source_message_id: int
    source_timestamp: datetime

    symbol_raw: str
    symbol_mt5: str | None

    direction: Direction
    order_type: OrderType

    entry: Decimal | None
    stop_loss: Decimal

    take_profits: tuple[Decimal, ...]  # immutable form of the spec's list[Decimal]
    selected_take_profit: Decimal

    parser_version: str
    raw_message: str

    def __post_init__(self) -> None:
        # Structural invariants only. Trading-rule validation (SL side, TP policy, …)
        # belongs to signals/validators.py (Phase 4).
        prices = [self.stop_loss, self.selected_take_profit, *self.take_profits]
        if self.entry is not None:
            prices.append(self.entry)
        if not all(isinstance(p, Decimal) and p.is_finite() for p in prices):
            raise TypeError("all prices must be finite Decimal (INV-07)")
        if self.source_timestamp.tzinfo is None:
            raise ValueError("source_timestamp must be timezone-aware")
        if not self.symbol_raw.strip():
            raise ValueError("symbol_raw is empty")
        if self.selected_take_profit not in self.take_profits:
            raise ValueError("selected_take_profit must be one of take_profits")
        if self.order_type.is_pending and self.entry is None:
            raise ValueError(f"{self.order_type} requires an entry price")
        implied = self.order_type.implied_direction
        if implied is not None and implied is not self.direction:
            raise ValueError(f"{self.order_type} contradicts direction {self.direction}")
