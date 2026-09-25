"""MT5 adapter against a fake MetaTrader5 module (real constant values).

Proves request construction, retcode validation and float->Decimal conversion. It does NOT
prove behaviour against a real terminal — that is tests/mt5_demo (Windows, Phase 12).
"""

from decimal import Decimal
from types import SimpleNamespace as NS
from typing import Any

import pytest
from pydantic import SecretStr

from plough_backer.enums import Direction, OrderType
from plough_backer.exceptions import (
    BrokerError,
    BrokerRejected,
    ExecutionFailed,
    MT5Unavailable,
    SymbolUnavailable,
)
from plough_backer.trading.gateway import OrderRequest
from plough_backer.trading.mt5_client import MT5Client

PASSWORD = "s3cret-pw"


class FakeMT5:
    # Values from the MetaTrader5 package.
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING = 1, 5
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 2, 3
    ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 4, 5
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL = 10008, 10009, 10010

    def __init__(self) -> None:
        self.connected = True
        self.init_ok = True
        self.sent: list[dict[str, Any]] = []
        self.send_result: Any = NS(
            retcode=10009,
            comment="Request executed",
            order=111,
            deal=222,
            price=4500.25,
            volume=0.03,
        )
        self.symbols = {
            "XAUUSDm": NS(
                name="XAUUSDm",
                volume_min=0.01,
                volume_max=200.0,
                volume_step=0.01,
                trade_tick_size=0.01,
                trade_tick_value=1.0,
                trade_contract_size=100.0,
                digits=2,
                point=0.01,
                trade_stops_level=0,
                trade_mode=4,
                filling_mode=2,
            ),
            "Volatility 75 Index": NS(
                name="Volatility 75 Index",
                volume_min=0.001,
                volume_max=1.0,
                volume_step=0.001,
                trade_tick_size=0.01,
                trade_tick_value=0.01,
                trade_contract_size=1.0,
                digits=2,
                point=0.01,
                trade_stops_level=0,
                trade_mode=4,
                filling_mode=1,
            ),
        }

    def initialize(self, *args: Any, **kwargs: Any) -> bool:
        self.init_kwargs = kwargs
        return self.init_ok

    def last_error(self) -> tuple[int, str]:
        return (-6, "Terminal: Authorization failed")

    def shutdown(self) -> None: ...

    def terminal_info(self) -> Any:
        return NS(connected=self.connected)

    def account_info(self) -> Any:
        return NS(balance=347.21, equity=341.8, margin=12.5, margin_free=329.3, currency="USD")

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return symbol in self.symbols

    def symbol_info(self, symbol: str) -> Any:
        return self.symbols.get(symbol)

    def symbol_info_tick(self, symbol: str) -> Any:
        return NS(bid=4500.10, ask=4500.30, time=1_790_000_000)

    def order_calc_profit(self, action: int, symbol: str, vol: float, o: float, c: float) -> float:
        return round((c - o) * 100 * vol * (1 if action == 0 else -1), 2)

    def order_send(self, request: dict[str, Any]) -> Any:
        self.sent.append(request)
        return self.send_result

    def positions_get(self) -> Any:
        return [NS(ticket=1, magic=777), NS(ticket=2, magic=0)]  # 2 = manual trade

    def history_deals_get(self, position: int) -> Any:
        return [
            NS(
                ticket=9,
                order=111,
                position_id=position,
                entry=1,
                reason=4,
                volume=0.03,
                price=4508.2,
                profit=23.7,
                swap=-0.1,
                commission=-0.21,
                fee=0.0,
                time=1_790_000_100,
                magic=777,
                comment="[tp 4508.20]",
            ),
        ]


def client(mt5: FakeMT5) -> MT5Client:
    return MT5Client(
        mt5,
        login=123,
        password=SecretStr(PASSWORD),
        server="Deriv-Demo",
        deviation_points=20,
        magic=777,
    )


def request(**over: Any) -> OrderRequest:
    fields: dict[str, Any] = {
        "symbol": "XAUUSDm",
        "direction": Direction.BUY,
        "order_type": OrderType.MARKET,
        "volume": Decimal("0.03"),
        "entry": Decimal("4500"),
        "stop_loss": Decimal("4492"),
        "take_profit": Decimal("4512"),
        "comment": "PB-000184",
    }
    return OrderRequest(**{**fields, **over})


def test_connect_failure_raises_without_leaking_password() -> None:
    mt5 = FakeMT5()
    mt5.init_ok = False
    with pytest.raises(MT5Unavailable) as exc:
        client(mt5).connect()
    assert PASSWORD not in str(exc.value)


def test_disconnected_terminal_blocks_everything() -> None:
    mt5 = FakeMT5()
    mt5.connected = False
    with pytest.raises(MT5Unavailable):
        client(mt5).submit_order(request())
    assert mt5.sent == []


def test_symbol_spec_is_exact_decimal() -> None:
    spec = client(FakeMT5()).symbol_specification("Volatility 75 Index")
    assert spec.volume_min == Decimal("0.001")
    assert str(spec.volume_step) == "0.001"
    assert spec.base_lot == Decimal("0.001")


def test_unknown_symbol_is_broker_error() -> None:
    with pytest.raises(SymbolUnavailable) as exc:
        client(FakeMT5()).symbol_specification("NOPE")
    assert isinstance(exc.value, BrokerError)


def test_account_snapshot() -> None:
    a = client(FakeMT5()).account_snapshot()
    assert (a.balance, a.equity, a.free_margin) == (
        Decimal("347.21"),
        Decimal("341.8"),
        Decimal("329.3"),
    )


def test_market_buy_request_uses_ask_and_supported_filling() -> None:
    mt5 = FakeMT5()
    result = client(mt5).submit_order(request())
    sent = mt5.sent[0]
    assert sent["action"] == FakeMT5.TRADE_ACTION_DEAL
    assert sent["type"] == FakeMT5.ORDER_TYPE_BUY
    assert sent["price"] == 4500.30  # ask for BUY
    assert sent["volume"] == 0.03
    assert (sent["sl"], sent["tp"]) == (4492.0, 4512.0)
    assert sent["type_filling"] == FakeMT5.ORDER_FILLING_IOC  # XAUUSDm allows IOC only
    assert sent["deviation"] == 20
    assert sent["magic"] == 777
    assert sent["comment"] == "PB-000184"
    assert result.order_id == 111
    assert result.deal_id == 222
    assert result.price == Decimal("4500.25")
    assert not result.partial_fill


def test_market_sell_uses_bid() -> None:
    mt5 = FakeMT5()
    client(mt5).submit_order(request(direction=Direction.SELL, stop_loss=Decimal("4508")))
    assert mt5.sent[0]["price"] == 4500.10
    assert mt5.sent[0]["type"] == FakeMT5.ORDER_TYPE_SELL


def test_pending_order_uses_entry_and_no_deviation() -> None:
    mt5 = FakeMT5()
    mt5.send_result = NS(retcode=10008, comment="placed", order=5, deal=0, price=0.0, volume=0.03)
    result = client(mt5).submit_order(request(order_type=OrderType.BUY_LIMIT))
    sent = mt5.sent[0]
    assert sent["action"] == FakeMT5.TRADE_ACTION_PENDING
    assert sent["type"] == FakeMT5.ORDER_TYPE_BUY_LIMIT
    assert sent["price"] == 4500.0
    assert "deviation" not in sent
    assert result.deal_id is None


@pytest.mark.parametrize("retcode", [10004, 10006, 10014, 10016, 10018, 10019])
def test_non_success_retcode_is_broker_rejected(retcode: int) -> None:
    # requote, rejected, invalid volume, invalid stops, market closed, no money
    mt5 = FakeMT5()
    mt5.send_result = NS(retcode=retcode, comment="nope", order=0, deal=0, price=0.0, volume=0.0)
    with pytest.raises(BrokerRejected) as exc:
        client(mt5).submit_order(request())
    assert exc.value.retcode == retcode


def test_pending_retcode_not_accepted_for_market_order() -> None:
    mt5 = FakeMT5()
    mt5.send_result = NS(retcode=10008, comment="placed", order=5, deal=0, price=0.0, volume=0.03)
    with pytest.raises(BrokerRejected):
        client(mt5).submit_order(request())


def test_order_send_none_is_execution_failed() -> None:
    mt5 = FakeMT5()
    mt5.send_result = None
    with pytest.raises(ExecutionFailed):
        client(mt5).submit_order(request())


def test_partial_fill_reports_actual_volume() -> None:
    mt5 = FakeMT5()
    mt5.send_result = NS(
        retcode=10010, comment="partial", order=1, deal=2, price=4500.3, volume=0.02
    )
    result = client(mt5).submit_order(request())
    assert result.partial_fill
    assert result.volume == Decimal("0.02")  # never pretend 0.03 executed (§17)


def test_calc_profit_is_decimal() -> None:
    loss = client(FakeMT5()).calc_profit(
        "XAUUSDm", Direction.BUY, Decimal("0.04"), Decimal("4500.20"), Decimal("4492.20")
    )
    assert loss == Decimal("-32.0")


def test_only_our_positions_are_listed() -> None:
    assert client(FakeMT5()).open_position_ids() == {1}


def test_history_deals_converted() -> None:
    (deal,) = client(FakeMT5()).deals_for_position(1)
    assert (deal.profit, deal.swap, deal.commission) == (
        Decimal("23.7"),
        Decimal("-0.1"),
        Decimal("-0.21"),
    )
    assert deal.position_id == 1
