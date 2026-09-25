"""Phase 8 settlement (§22, §29, §65) + Phase 10 reports from settled trades."""

import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select

from plough_backer import controls
from plough_backer.analytics.reports import (
    monthly_report,
    previous_month,
    previous_week,
    weekly_report,
)
from plough_backer.enums import RiskMode, SignalState, TradeOutcome
from plough_backer.persistence.database import session_factory
from plough_backer.persistence.models import Signal, Trade
from plough_backer.telegram.notifications import trade_closed
from plough_backer.trading.gateway import Deal
from plough_backer.trading.settlement import SettlementPolicy, Settler
from tests.fakes import FakeGateway
from tests.integration.test_orchestrator import orchestrator, progression, send

D = Decimal
CLOSE = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
POLICY = SettlementPolicy(breakeven_tolerance=D("0.50"), manual_close_is_other=True)
SL, TP, CLIENT = 4, 5, 0


def deal(position: int, *, entry: int, profit: str, reason: int = TP, ticket: int = 1) -> Deal:
    return Deal(
        ticket=ticket,
        order_id=position,
        position_id=position,
        entry=entry,
        reason=reason,
        volume=D("0.01"),
        price=D("4512") if reason == TP else D("4492"),
        profit=D(profit),
        swap=D(0),
        commission=D("-0.05"),
        fee=D(0),
        time=CLOSE,
        magic=777,
        comment="",
    )


def open_and_close(engine: Engine, gw: FakeGateway, profit: str, reason: int = TP) -> int:
    out = send(orchestrator(engine, gw))
    pid = out.result.order_id
    gw.deals[pid] = [
        deal(pid, entry=0, profit="0", ticket=1),
        deal(pid, entry=1, profit=profit, reason=reason, ticket=2),
    ]
    return pid


def settler(engine: Engine, gw: FakeGateway) -> Settler:
    return Settler(sessions=session_factory(engine), gateway=gw, policy=POLICY)


def test_tp_win_settles_and_advances_progression(engine: Engine, gw: FakeGateway) -> None:
    open_and_close(engine, gw, "12")
    (st,) = settler(engine, gw).reconcile()
    assert st.outcome is TradeOutcome.WIN
    assert st.net_profit == D("11.90")  # 12 - 0.05 entry commission - 0.05 exit commission
    assert (st.previous_lot, st.next_lot) == (D("0.01"), D("0.02"))  # Mode 1: W -> x2
    assert st.r_multiple == D("11.90") / D("8.00")
    state, version = progression(engine) or (None, 0)
    assert state is not None
    assert (state.theoretical_lot, state.wins, version) == (D("0.02"), 1, 2)
    with session_factory(engine).begin() as s:
        t = s.scalar(select(Trade))
        signal = s.scalar(select(Signal))
        assert t is not None
        assert signal is not None
        assert (t.result, t.close_price, t.settled_at) == ("WIN", D("4512"), CLOSE)
        assert signal.state == SignalState.CLOSED_WIN


def test_same_closed_deal_seen_repeatedly_settles_once(engine: Engine, gw: FakeGateway) -> None:
    open_and_close(engine, gw, "12")
    s = settler(engine, gw)
    assert len(s.reconcile()) == 1
    for _ in range(5):  # §65: MT5 keeps returning the same closed deal
        assert s.reconcile() == []
    state, version = progression(engine) or (None, 0)
    assert state is not None
    assert (state.theoretical_lot, version) == (D("0.02"), 2)


def test_concurrent_reconciliation_settles_once(engine: Engine, gw: FakeGateway) -> None:
    open_and_close(engine, gw, "12")
    results: list[int] = []
    threads = [
        threading.Thread(target=lambda: results.append(len(settler(engine, gw).reconcile())))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 1
    assert (progression(engine) or (None, 0))[1] == 2


def test_sl_loss_resets_mode1(engine: Engine, gw: FakeGateway) -> None:
    open_and_close(engine, gw, "-8", reason=SL)
    (st,) = settler(engine, gw).reconcile()
    assert st.outcome is TradeOutcome.LOSS
    assert (st.previous_lot, st.next_lot) == (D("0.01"), D("0.01"))


def test_still_open_position_is_not_settled(engine: Engine, gw: FakeGateway) -> None:
    pid = open_and_close(engine, gw, "12")
    gw.open_positions = {pid}
    assert settler(engine, gw).reconcile() == []


def test_missing_position_without_deals_is_not_guessed(engine: Engine, gw: FakeGateway) -> None:
    pid = open_and_close(engine, gw, "12")
    gw.deals[pid] = []
    assert settler(engine, gw).reconcile() == []
    assert progression(engine) is not None
    assert (progression(engine) or (None, 0))[1] == 1


def test_manual_close_is_other_flagged_and_progression_unchanged(
    engine: Engine, gw: FakeGateway
) -> None:
    open_and_close(engine, gw, "5", reason=CLIENT)
    (st,) = settler(engine, gw).reconcile()
    assert st.outcome is TradeOutcome.OTHER
    assert st.needs_review
    assert (progression(engine) or (None, 0))[1] == 1  # §22 OTHER -> unchanged + review


def test_breakeven_within_tolerance_leaves_progression(engine: Engine, gw: FakeGateway) -> None:
    open_and_close(engine, gw, "0.30")
    (st,) = settler(engine, gw).reconcile()
    assert st.outcome is TradeOutcome.BREAKEVEN
    assert not st.needs_review
    assert (progression(engine) or (None, 0))[1] == 1


def test_mode_changed_while_open_is_flagged_not_applied(engine: Engine, gw: FakeGateway) -> None:
    open_and_close(engine, gw, "12")
    with session_factory(engine).begin() as s:
        controls.change_risk_mode(s, RiskMode.ALWAYS_DOUBLE, actor="42")
    (st,) = settler(engine, gw).reconcile()
    assert st.needs_review
    assert st.previous_lot is None
    state, _ = progression(engine) or (None, 0)
    assert state is not None
    assert state.theoretical_lot == D("0.01")  # new method untouched by old trade


def test_closure_notification(engine: Engine, gw: FakeGateway) -> None:
    open_and_close(engine, gw, "12")
    (st,) = settler(engine, gw).reconcile()
    text = trade_closed(st, RiskMode.ANTI_MARTINGALE)
    assert text.startswith("✅ TRADE CLOSED — WIN")
    assert "P/L:       +$11.90" in text
    assert "Previous theoretical lot: 0.0100" in text
    assert "Next theoretical lot:     0.0200" in text


# --- reports (Phase 10) -------------------------------------------------------------------


def test_period_helpers() -> None:
    start, end = previous_week(date(2026, 9, 24), "UTC")  # Thursday
    assert (start.date().isoformat(), end.date().isoformat()) == ("2026-09-14", "2026-09-21")
    start, end = previous_month(date(2026, 9, 24), "UTC")
    assert (start.date().isoformat(), end.date().isoformat()) == ("2026-08-01", "2026-09-01")


@pytest.mark.parametrize("builder", [weekly_report, monthly_report])
def test_reports_from_settled_trades(engine: Engine, gw: FakeGateway, builder: object) -> None:
    open_and_close(engine, gw, "12")
    settler(engine, gw).reconcile()
    start, end = CLOSE - timedelta(days=3), CLOSE + timedelta(days=4)
    with session_factory(engine).begin() as s:
        text = builder(s, start, end, RiskMode.ANTI_MARTINGALE)  # type: ignore[operator]
    for part in (
        "Opening Balance: $800.00",
        "Closing Balance: $811.90",
        "Net P/L: $11.90",
        "Total: 1",
        "Wins:  1",
        "XAUUSD:",
        "1 trades | 1W / 0L",
    ):
        assert part in text
    if builder is monthly_report:
        assert "BY SOURCE" in text
        assert "Parser rejections:       0" in text
        assert "Avg execution latency:" in text
