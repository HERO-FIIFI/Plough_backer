"""MetaTrader 5 adapter implementing BrokerGateway (README §13, §28, §29, Phase 5).

Contains no progression, sizing or eligibility logic. Floats from MT5 are converted to
Decimal at this boundary; Decimals are converted to float only when calling MT5.

The MetaTrader5 module is injected (`MT5Client(load_mt5(), ...)`) so this file is testable
off-Windows with a fake. The real package only runs on Windows next to the terminal (§73).
"""

import importlib
from datetime import UTC, datetime
from decimal import Decimal
from types import ModuleType
from typing import Any

from pydantic import SecretStr

from plough_backer.decimal_utils import to_decimal
from plough_backer.enums import Direction, OrderType
from plough_backer.exceptions import (
    BrokerRejected,
    ExecutionFailed,
    MT5Unavailable,
    SymbolUnavailable,
)
from plough_backer.trading.gateway import (
    AccountSnapshot,
    Deal,
    OrderRequest,
    OrderResult,
    SymbolSpecification,
    Tick,
)

_FILLING_FOK_FLAG, _FILLING_IOC_FLAG = 1, 2  # SYMBOL_FILLING_* bits in symbol_info.filling_mode


def load_mt5() -> ModuleType:
    """Import the Windows-only MetaTrader5 package (pip install -e ".[mt5]")."""
    return importlib.import_module("MetaTrader5")


def _time(epoch: float) -> datetime:
    # ponytail: MT5 reports times as epoch seconds in the TRADE SERVER's timezone, not true UTC.
    # Treated as UTC here; Phase 5 demo check must measure the broker offset before reports.
    return datetime.fromtimestamp(epoch, UTC)


class MT5Client:
    def __init__(
        self,
        mt5: Any,
        *,
        login: int,
        password: SecretStr,
        server: str,
        deviation_points: int,  # max slippage for market orders — a trading choice, no default
        magic: int,  # tags our orders so reconciliation can tell them from manual trades
        terminal_path: str | None = None,
    ) -> None:
        self._mt5 = mt5
        self._login = login
        self._password = password
        self._server = server
        self._deviation = deviation_points
        self._magic = magic
        self._path = terminal_path

    # --- connection -----------------------------------------------------------------------

    def connect(self) -> None:
        kwargs: dict[str, Any] = {
            "login": self._login,
            "password": self._password.get_secret_value(),
            "server": self._server,
        }
        ok = (
            self._mt5.initialize(self._path, **kwargs)
            if self._path
            else self._mt5.initialize(**kwargs)
        )
        if not ok:
            # last_error() is (code, text) and never contains the password.
            raise MT5Unavailable(f"MT5 initialize failed: {self._mt5.last_error()}")

    def shutdown(self) -> None:
        self._mt5.shutdown()

    def is_connected(self) -> bool:
        info = self._mt5.terminal_info()
        return info is not None and bool(info.connected)

    def _require_connection(self) -> None:
        if not self.is_connected():
            raise MT5Unavailable("MT5 terminal not connected")

    def _error(self, what: str) -> str:
        return f"{what}: {self._mt5.last_error()}"

    # --- reads ----------------------------------------------------------------------------

    def account_snapshot(self) -> AccountSnapshot:
        self._require_connection()
        a = self._mt5.account_info()
        if a is None:
            raise MT5Unavailable(self._error("account_info failed"))
        return AccountSnapshot(
            balance=to_decimal(a.balance),
            equity=to_decimal(a.equity),
            margin=to_decimal(a.margin),
            free_margin=to_decimal(a.margin_free),
            currency=a.currency,
        )

    def symbol_specification(self, symbol: str) -> SymbolSpecification:
        self._require_connection()
        if not self._mt5.symbol_select(symbol, True):  # §28 step 3: visible/selectable
            raise SymbolUnavailable(self._error(f"symbol_select({symbol!r}) failed"))
        s = self._mt5.symbol_info(symbol)
        if s is None:
            raise SymbolUnavailable(self._error(f"symbol_info({symbol!r}) failed"))
        return SymbolSpecification(
            name=s.name,
            volume_min=to_decimal(s.volume_min),
            volume_max=to_decimal(s.volume_max),
            volume_step=to_decimal(s.volume_step),
            trade_tick_size=to_decimal(s.trade_tick_size),
            trade_tick_value=to_decimal(s.trade_tick_value),
            trade_contract_size=to_decimal(s.trade_contract_size),
            digits=s.digits,
            point=to_decimal(s.point),
            trade_stops_level=s.trade_stops_level,
            trade_mode=s.trade_mode,
        )

    def current_tick(self, symbol: str) -> Tick:
        self._require_connection()
        t = self._mt5.symbol_info_tick(symbol)
        if t is None:
            raise ExecutionFailed(self._error(f"no tick for {symbol!r}"))
        return Tick(bid=to_decimal(t.bid), ask=to_decimal(t.ask), time=_time(t.time))

    def calc_profit(
        self,
        symbol: str,
        direction: Direction,
        volume: Decimal,
        price_open: Decimal,
        price_close: Decimal,
    ) -> Decimal:
        self._require_connection()
        action = (
            self._mt5.ORDER_TYPE_BUY if direction is Direction.BUY else self._mt5.ORDER_TYPE_SELL
        )
        profit = self._mt5.order_calc_profit(
            action, symbol, float(volume), float(price_open), float(price_close)
        )
        if profit is None:
            raise ExecutionFailed(self._error("order_calc_profit failed"))
        return to_decimal(profit)

    def open_position_ids(self) -> set[int]:
        self._require_connection()
        positions = self._mt5.positions_get()
        if positions is None:
            raise ExecutionFailed(self._error("positions_get failed"))
        return {p.ticket for p in positions if p.magic == self._magic}

    def deals_for_position(self, position_id: int) -> list[Deal]:
        self._require_connection()
        deals = self._mt5.history_deals_get(position=position_id)
        if deals is None:
            raise ExecutionFailed(self._error(f"history_deals_get({position_id}) failed"))
        return [
            Deal(
                ticket=d.ticket,
                order_id=d.order,
                position_id=d.position_id,
                entry=d.entry,
                reason=d.reason,
                volume=to_decimal(d.volume),
                price=to_decimal(d.price),
                profit=to_decimal(d.profit),
                swap=to_decimal(d.swap),
                commission=to_decimal(d.commission),
                fee=to_decimal(d.fee),
                time=_time(d.time),
                magic=d.magic,
                comment=d.comment,
            )
            for d in deals
        ]

    # --- orders ---------------------------------------------------------------------------

    def _mt5_order_type(self, order_type: OrderType, direction: Direction) -> int:
        if order_type is OrderType.MARKET:
            name = "ORDER_TYPE_BUY" if direction is Direction.BUY else "ORDER_TYPE_SELL"
        else:
            name = f"ORDER_TYPE_{order_type.value}"
        return int(getattr(self._mt5, name))

    def _filling(self, symbol: str, pending: bool) -> int:
        if pending:
            return int(self._mt5.ORDER_FILLING_RETURN)
        info = self._mt5.symbol_info(symbol)
        mode = info.filling_mode if info is not None else 0
        if mode & _FILLING_FOK_FLAG:
            return int(self._mt5.ORDER_FILLING_FOK)
        if mode & _FILLING_IOC_FLAG:
            return int(self._mt5.ORDER_FILLING_IOC)
        return int(self._mt5.ORDER_FILLING_RETURN)

    def build_request(self, req: OrderRequest) -> dict[str, Any]:
        pending = req.order_type.is_pending
        if pending:
            if req.entry is None:
                raise ValueError("pending order requires entry")
            price = req.entry
        else:
            tick = self.current_tick(req.symbol)  # §28 step 5
            price = tick.ask if req.direction is Direction.BUY else tick.bid
        request: dict[str, Any] = {
            "action": self._mt5.TRADE_ACTION_PENDING if pending else self._mt5.TRADE_ACTION_DEAL,
            "symbol": req.symbol,
            "volume": float(req.volume),
            "type": self._mt5_order_type(req.order_type, req.direction),
            "price": float(price),
            "sl": float(req.stop_loss),
            "tp": float(req.take_profit),
            "magic": self._magic,
            "comment": req.comment[:31],  # MT5 comment limit
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(req.symbol, pending),
        }
        if not pending:
            request["deviation"] = self._deviation
        return request

    def submit_order(self, req: OrderRequest) -> OrderResult:
        self._require_connection()
        request = self.build_request(req)
        sent_at = datetime.now(UTC)
        result = self._mt5.order_send(request)
        response_at = datetime.now(UTC)
        if result is None:  # never assume success from "an object came back" (§28)
            raise ExecutionFailed(self._error("order_send returned None"))

        ok = {self._mt5.TRADE_RETCODE_DONE, self._mt5.TRADE_RETCODE_DONE_PARTIAL}
        if req.order_type.is_pending:
            ok = {self._mt5.TRADE_RETCODE_PLACED, self._mt5.TRADE_RETCODE_DONE}
        if result.retcode not in ok:
            raise BrokerRejected(result.retcode, result.comment)

        filled = to_decimal(result.volume)
        return OrderResult(
            retcode=result.retcode,
            message=result.comment,
            order_id=result.order,
            deal_id=result.deal or None,
            price=to_decimal(result.price) if result.price else None,
            volume=filled,
            partial_fill=result.retcode == self._mt5.TRADE_RETCODE_DONE_PARTIAL,
            request_sent_at=sent_at,
            response_at=response_at,
        )
