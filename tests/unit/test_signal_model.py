from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from plough_backer.enums import Direction, OrderType
from plough_backer.signals.models import NormalizedSignal

D = Decimal


def _signal(**overrides: Any) -> NormalizedSignal:
    fields: dict[str, Any] = {
        "signal_id": "PB-000001",
        "source_id": "gold_signals",
        "source_message_id": 42,
        "source_timestamp": datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
        "symbol_raw": "GOLD",
        "symbol_mt5": "XAUUSDm",
        "direction": Direction.BUY,
        "order_type": OrderType.MARKET,
        "entry": D("4500"),
        "stop_loss": D("4492"),
        "take_profits": (D("4506"), D("4512"), D("4518")),
        "selected_take_profit": D("4512"),
        "parser_version": "test-1",
        "raw_message": "BUY GOLD 4500\nSL 4492\nTP1 4506\nTP2 4512\nTP3 4518",
    }
    fields.update(overrides)
    return NormalizedSignal(**fields)


def test_valid_signal_is_immutable() -> None:
    s = _signal()
    with pytest.raises(FrozenInstanceError):
        s.stop_loss = D("1")  # type: ignore[misc]


def test_market_order_may_omit_entry() -> None:
    assert _signal(entry=None).entry is None


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"stop_loss": 4492.0}, TypeError),  # float price violates INV-07
        ({"entry": D("NaN")}, TypeError),
        ({"selected_take_profit": D("4520")}, ValueError),
        ({"take_profits": ()}, ValueError),
        ({"order_type": OrderType.BUY_LIMIT, "entry": None}, ValueError),
        ({"order_type": OrderType.SELL_STOP}, ValueError),  # contradicts BUY
        ({"source_timestamp": datetime(2026, 9, 24)}, ValueError),  # noqa: DTZ001 — naive on purpose
        ({"symbol_raw": "  "}, ValueError),
    ],
)
def test_structural_invariants(overrides: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        _signal(**overrides)


def test_order_type_direction_mapping() -> None:
    assert OrderType.MARKET.implied_direction is None
    assert OrderType.BUY_STOP.implied_direction is Direction.BUY
    assert OrderType.SELL_LIMIT.implied_direction is Direction.SELL
    assert not OrderType.MARKET.is_pending
