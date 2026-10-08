"""Test double for BrokerGateway. Tick-based P/L, like MT5's order_calc_profit for CFDs."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from plough_backer.enums import Direction
from plough_backer.trading.gateway import (
    AccountSnapshot,
    Deal,
    OrderRequest,
    OrderResult,
    SymbolSpecification,
    Tick,
)

D = Decimal


def spec(
    name: str, *, vmin: str, vmax: str, step: str, tick: str, tick_value: str
) -> SymbolSpecification:
    return SymbolSpecification(
        name=name,
        volume_min=D(vmin),
        volume_max=D(vmax),
        volume_step=D(step),
        trade_tick_size=D(tick),
        trade_tick_value=D(tick_value),
        trade_contract_size=D(100),
        digits=2,
        point=D(tick),
        trade_stops_level=0,
        trade_mode=4,
    )


# XAUUSD: 1 lot = 100 oz, $1 per 0.01 move per lot -> matches README §40's numbers.
GOLD = spec("XAUUSD", vmin="0.01", vmax="100", step="0.01", tick="0.01", tick_value="1")
# Deriv-style synthetics (illustrative specs — real ones come from MT5 in Phase 5).
V75 = spec(
    "Volatility 75 Index", vmin="0.001", vmax="1", step="0.001", tick="0.01", tick_value="0.01"
)
BOOM = spec("Boom 1000 Index", vmin="0.5", vmax="50", step="0.01", tick="0.001", tick_value="0.01")


@dataclass
class FakeGateway:
    specs: dict[str, SymbolSpecification] = field(
        default_factory=lambda: {s.name: s for s in (GOLD, V75, BOOM)}
    )
    account: AccountSnapshot = field(
        default_factory=lambda: AccountSnapshot(
            balance=D(800), equity=D(800), margin=D(0), free_margin=D(800), currency="USD"
        )
    )
    account_snapshots: list[AccountSnapshot] = field(default_factory=list)
    connected: bool = True
    bid: Decimal = field(default_factory=lambda: D("4500.10"))
    ask: Decimal = field(default_factory=lambda: D("4500.30"))
    fail_with: Exception | None = None  # raised by submit_order
    failures: list[Exception] = field(default_factory=list)  # one failure per attempted order
    orders: list[OrderRequest] = field(default_factory=list)
    _next_ticket: int = 1000

    def current_tick(self, symbol: str) -> Tick:
        return Tick(self.bid, self.ask, datetime(2026, 9, 24, tzinfo=UTC))

    def submit_order(self, request: OrderRequest) -> OrderResult:
        self.orders.append(request)
        if self.failures:
            raise self.failures.pop(0)
        if self.fail_with is not None:
            raise self.fail_with
        self._next_ticket += 1
        now = datetime(2026, 9, 24, 10, 0, 1, tzinfo=UTC)
        pending = request.order_type.is_pending
        price = (
            request.entry
            if pending
            else (self.ask if request.direction is Direction.BUY else self.bid)
        )
        return OrderResult(
            retcode=10008 if pending else 10009,
            message="done",
            order_id=self._next_ticket,
            deal_id=None if pending else self._next_ticket + 50000,
            price=price,
            volume=request.volume,
            partial_fill=False,
            request_sent_at=now,
            response_at=now,
        )

    open_positions: set[int] = field(default_factory=set)
    deals: dict[int, list[Deal]] = field(default_factory=dict)

    def open_position_ids(self) -> set[int]:
        return set(self.open_positions)

    def deals_for_position(self, position_id: int) -> list[Deal]:
        return list(self.deals.get(position_id, []))

    def is_connected(self) -> bool:
        return self.connected

    def account_snapshot(self) -> AccountSnapshot:
        if self.account_snapshots:
            return self.account_snapshots.pop(0)
        return self.account

    def symbol_specification(self, symbol: str) -> SymbolSpecification:
        return self.specs[symbol]

    def calc_profit(
        self,
        symbol: str,
        direction: Direction,
        volume: Decimal,
        price_open: Decimal,
        price_close: Decimal,
    ) -> Decimal:
        s = self.specs[symbol]
        move = price_close - price_open if direction is Direction.BUY else price_open - price_close
        return move / s.trade_tick_size * s.trade_tick_value * volume
