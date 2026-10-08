"""Phase 6: every lifecycle exit, idempotency (§64), crash safety (INV-15), INV-02..05."""

import threading
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, func, select

from plough_backer import controls
from plough_backer.config import SourcesConfig, SymbolsConfig
from plough_backer.enums import (
    AuditEventType,
    ExecutionMode,
    ProgressionScope,
    RiskEntrySource,
    RiskMode,
    SignalState,
    TradingStatus,
    VolumeMaxPolicy,
)
from plough_backer.exceptions import BrokerRejected
from plough_backer.persistence import repositories as repo
from plough_backer.persistence.database import make_engine, session_factory
from plough_backer.persistence.models import AuditEvent, Trade
from plough_backer.risk.progression import ProgressionState
from plough_backer.trading.executor import ExecutionPolicy, Orchestrator, scope_key
from tests.fakes import FakeGateway

D = Decimal
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
GOLD_MSG = "BUY GOLD 4500\nSL 4492\nTP1 4506\nTP2 4512\nTP3 4518"
SYMBOLS = SymbolsConfig.model_validate(
    {
        "symbols": {
            "GOLD": {"aliases": ["GOLD", "XAUUSD"], "mt5_symbol": "XAUUSD", "asset_class": "GOLD"}
        }
    }
)
SOURCES = SourcesConfig.model_validate(
    {
        "sources": [
            {
                "id": "gold",
                "name": "Gold Signals",
                "telegram_chat_id": -100,
                "parser_profile": "standard_v1",
            }
        ]
    }
)
KEY = scope_key(ProgressionScope.PER_SOURCE, "gold", "XAUUSD")


def orchestrator(engine: Engine, gw: FakeGateway | None, **policy: Any) -> Orchestrator:
    p: dict[str, Any] = {
        "execution_mode": ExecutionMode.DEMO,
        "progression_scope": ProgressionScope.PER_SOURCE,
        "volume_max_policy": VolumeMaxPolicy.REJECT,
        "risk_entry": RiskEntrySource.SIGNAL_ENTRY_OR_LIVE,
        **policy,
    }
    return Orchestrator(
        sessions=session_factory(engine),
        gateway=gw,
        symbols=SYMBOLS,
        sources=SOURCES,
        policy=ExecutionPolicy(**p),
        clock=lambda: NOW,
    )


def send(o: Orchestrator, text: str = GOLD_MSG, message_id: int = 1, source: str = "gold") -> Any:
    return o.process_message(
        source_id=source, message_id=message_id, message_time=NOW, text=text, received_at=NOW
    )


def trades(engine: Engine) -> list[Trade]:
    with session_factory(engine).begin() as s:
        return list(s.scalars(select(Trade)).all())


def progression(engine: Engine) -> tuple[ProgressionState, int] | None:
    with session_factory(engine).begin() as s:
        return repo.load_progression(s, KEY)


# --- happy path ---------------------------------------------------------------------------


def test_valid_gold_signal_executes_at_tp2(engine: Engine, gw: FakeGateway) -> None:
    out = send(orchestrator(engine, gw))
    assert out.state is SignalState.OPEN
    assert out.signal_id == "PB-000001"
    (order,) = gw.orders
    assert (order.take_profit, order.volume, order.comment) == (D("4512"), D("0.01"), "PB-000001")
    (t,) = trades(engine)
    assert (t.tp1, t.tp2, t.tp3, t.selected_tp) == (D("4506"), D("4512"), D("4518"), D("4512"))
    assert (t.base_lot, t.theoretical_lot, t.executed_lot) == (D("0.01"),) * 3
    assert t.estimated_risk == D("8.00")  # (4500-4492)/0.01 * $1 * 0.01 lot
    assert t.mt5_position_id == out.result.order_id
    assert t.equity_before == D(800)
    # INV-02: ordinary execution does not move progression — only settlement does.
    assert progression(engine) == (ProgressionState.initial(RiskMode.ANTI_MARTINGALE, D("0.01")), 1)


def test_live_price_risk_policy_uses_ask(engine: Engine, gw: FakeGateway) -> None:
    out = send(orchestrator(engine, gw, risk_entry=RiskEntrySource.LIVE_PRICE))
    assert out.risk.risk_to_sl == D("8.30")  # (4500.30 - 4492) at 0.01 lot


# --- idempotency / crash safety -----------------------------------------------------------


def test_same_message_executes_once_across_restart(
    engine: Engine, db_url: str, gw: FakeGateway
) -> None:
    send(orchestrator(engine, gw))
    assert send(orchestrator(engine, gw)).state is SignalState.REJECTED_DUPLICATE
    engine.dispose()
    engine2 = make_engine(db_url)  # §64: crash + restart + message seen again
    assert send(orchestrator(engine2, gw)).state is SignalState.REJECTED_DUPLICATE
    assert len(gw.orders) == 1
    assert len(trades(engine2)) == 1
    engine2.dispose()


def test_crash_between_claim_and_response_never_resubmits(
    engine: Engine, db_url: str, gw: FakeGateway
) -> None:
    gw.fail_with = RuntimeError("process killed mid order_send")
    with pytest.raises(RuntimeError):
        send(orchestrator(engine, gw))
    (t,) = trades(engine)  # the claim was committed before sending
    assert t.execution_retcode is None
    gw.fail_with = None
    engine2 = make_engine(db_url)
    out = send(orchestrator(engine2, gw))
    assert out.state is SignalState.REJECTED_DUPLICATE
    assert len(gw.orders) == 1  # only the attempt that "crashed"
    engine2.dispose()


def test_concurrent_messages_both_execute(engine: Engine, gw: FakeGateway) -> None:
    o = orchestrator(engine, gw)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda i=i: results.append(send(o, message_id=i))) for i in (1, 2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.state for r in results) == [SignalState.OPEN, SignalState.OPEN]
    assert len(gw.orders) == 2


# --- non-execution exits: progression must stay untouched (INV-03/04/05) ------------------


def assert_no_trade_and_progression_untouched(engine: Engine, gw: FakeGateway) -> None:
    assert gw.orders == []
    assert trades(engine) == []
    assert progression(engine) in (
        None,
        (ProgressionState.initial(RiskMode.ANTI_MARTINGALE, D("0.01")), 1),
    )


def test_equity_lock_blocks(engine: Engine, gw: FakeGateway) -> None:
    with session_factory(engine).begin() as s:
        controls.set_equity_lock(s, enabled=True, value=D("795"), actor="42")
    out = send(orchestrator(engine, gw))  # 800 - 8 = 792 < 795
    assert out.state is SignalState.BLOCKED_EQUITY_LOCK
    assert out.lock.projected_equity == D("792.00")
    assert gw.orders == []
    assert_no_trade_and_progression_untouched(engine, gw)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (TradingStatus.PAUSED, SignalState.SKIPPED_PAUSED),
        (TradingStatus.STOPPED, SignalState.SKIPPED_STOPPED),
    ],
)
def test_pause_and_stop_skip_execution(
    engine: Engine, gw: FakeGateway, status: TradingStatus, expected: SignalState
) -> None:
    with session_factory(engine).begin() as s:
        controls.set_trading_status(s, status, actor="42")
    assert send(orchestrator(engine, gw)).state is expected
    assert gw.orders == []
    assert_no_trade_and_progression_untouched(engine, gw)


def test_mt5_down_skips_and_never_replays(engine: Engine, gw: FakeGateway) -> None:
    gw.connected = False
    assert send(orchestrator(engine, gw)).state is SignalState.SKIPPED_MT5_UNAVAILABLE
    gw.connected = True
    assert send(orchestrator(engine, gw)).state is SignalState.REJECTED_DUPLICATE  # §57
    assert gw.orders == []


def test_no_money_resets_progression_and_retries_once_at_base_lot(
    engine: Engine, gw: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    """Catches leaving the unaffordable progression in place or retrying the same large lot."""
    progressed = ProgressionState(
        mode=RiskMode.ANTI_MARTINGALE,
        base_lot=D("0.01"),
        theoretical_lot=D("0.16"),
        wins=4,
    )
    unrelated_key = "PER_SOURCE:other:XAUUSD"
    with session_factory(engine).begin() as s:
        repo.create_progression(s, KEY, progressed)
        repo.create_progression(s, unrelated_key, progressed)
    gw.failures = [BrokerRejected(10019, "No money")]

    with caplog.at_level("WARNING"):
        out = send(orchestrator(engine, gw))

    assert out.state is SignalState.OPEN
    assert [order.volume for order in gw.orders] == [D("0.16"), D("0.01")]
    assert out.extra["progression_reset"] == {
        "reason": "INSUFFICIENT_MARGIN",
        "old_theoretical_lot": D("0.16"),
        "new_theoretical_lot": D("0.01"),
        "retried": True,
    }
    (trade,) = trades(engine)
    assert (trade.theoretical_lot, trade.executed_lot, trade.execution_retcode) == (
        D("0.01"),
        D("0.01"),
        10009,
    )
    assert progression(engine) == (
        ProgressionState.initial(RiskMode.ANTI_MARTINGALE, D("0.01")),
        2,
    )
    with session_factory(engine).begin() as s:
        unrelated = repo.load_progression(s, unrelated_key)
        events = list(s.scalars(select(AuditEvent).order_by(AuditEvent.id)).all())
    assert unrelated == (progressed, 1)
    rejected = next(e for e in events if e.event_type == AuditEventType.TRADE_REJECTED)
    assert rejected.payload == {
        "signal_id": "PB-000001",
        "retcode": 10019,
        "broker_message": "No money",
        "rejected_lot": "0.16",
        "balance": "800",
        "equity": "800",
        "free_margin": "800",
    }
    reset = next(e for e in events if e.event_type == AuditEventType.PROGRESSION_RESET)
    assert reset.payload["reason"] == "INSUFFICIENT_MARGIN"
    assert reset.payload["old_theoretical_lot"] == "0.16"
    assert reset.payload["new_theoretical_lot"] == "0.01"
    record = next(r for r in caplog.records if r.message == "insufficient_margin_progression_reset")
    assert (record.account_id, record.retcode, record.rejected_lot) == ("default", 10019, D("0.16"))


def test_no_money_retry_stops_after_one_attempt(engine: Engine, gw: FakeGateway) -> None:
    with session_factory(engine).begin() as s:
        repo.create_progression(
            s,
            KEY,
            ProgressionState(
                mode=RiskMode.ANTI_MARTINGALE,
                base_lot=D("0.01"),
                theoretical_lot=D("0.16"),
                wins=4,
            ),
        )
    gw.fail_with = BrokerRejected(10019, "No money")

    out = send(orchestrator(engine, gw))

    assert out.state is SignalState.BROKER_REJECTED
    assert [order.volume for order in gw.orders] == [D("0.16"), D("0.01")]
    assert out.extra["progression_reset"]["retried"] is True
    assert progression(engine) == (
        ProgressionState.initial(RiskMode.ANTI_MARTINGALE, D("0.01")),
        2,
    )


def test_no_money_reset_rechecks_equity_lock_before_retry(
    engine: Engine, gw: FakeGateway
) -> None:
    with session_factory(engine).begin() as s:
        repo.create_progression(
            s,
            KEY,
            ProgressionState(
                mode=RiskMode.ANTI_MARTINGALE,
                base_lot=D("0.01"),
                theoretical_lot=D("0.16"),
                wins=4,
            ),
        )
        controls.set_equity_lock(s, enabled=True, value=D("20"), actor="42")
    gw.failures = [BrokerRejected(10019, "No money")]
    gw.account_snapshots = [
        gw.account,
        replace(gw.account, balance=D("25"), equity=D("25"), free_margin=D("25")),
    ]

    out = send(orchestrator(engine, gw))

    assert out.state is SignalState.BLOCKED_EQUITY_LOCK
    assert [order.volume for order in gw.orders] == [D("0.16")]
    assert out.extra["progression_reset"]["retried"] is False
    assert progression(engine) == (
        ProgressionState.initial(RiskMode.ANTI_MARTINGALE, D("0.01")),
        2,
    )


def test_other_broker_rejection_is_recorded_without_reset_or_retry(
    engine: Engine, gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    progressed = ProgressionState(
        mode=RiskMode.ANTI_MARTINGALE,
        base_lot=D("0.01"),
        theoretical_lot=D("0.16"),
        wins=4,
    )
    with session_factory(engine).begin() as s:
        repo.create_progression(s, KEY, progressed)
    gw.fail_with = BrokerRejected(10018, "Market closed")
    snapshots = 0

    def account_snapshot() -> Any:
        nonlocal snapshots
        snapshots += 1
        if snapshots > 1:
            raise RuntimeError("broker unavailable after rejection")
        return gw.account

    monkeypatch.setattr(gw, "account_snapshot", account_snapshot)

    out = send(orchestrator(engine, gw))

    assert out.state is SignalState.BROKER_REJECTED
    assert [order.volume for order in gw.orders] == [D("0.16")]
    assert "progression_reset" not in out.extra
    assert progression(engine) == (progressed, 1)


@pytest.mark.parametrize("policy", [VolumeMaxPolicy.REJECT, VolumeMaxPolicy.CAP])
@pytest.mark.parametrize("theoretical", ["100", "500"])
def test_progression_at_or_above_broker_max_resets_before_execution(
    engine: Engine, gw: FakeGateway, policy: VolumeMaxPolicy, theoretical: str
) -> None:
    """Catches rejecting/capping a progressed lot instead of starting a fresh cycle."""
    with session_factory(engine).begin() as s:
        state = ProgressionState(
            mode=RiskMode.ANTI_MARTINGALE,
            base_lot=D("0.01"),
            theoretical_lot=D(theoretical),
            wins=12,
            losses=3,
        )
        repo.create_progression(s, KEY, state)
    out = send(orchestrator(engine, gw, volume_max_policy=policy))
    assert out.state is SignalState.OPEN
    (t,) = trades(engine)
    assert (t.base_lot, t.theoretical_lot, t.executed_lot, t.volume_capped_at_max) == (
        D("0.01"),
        D("0.01"),
        D("0.01"),
        False,
    )
    assert progression(engine) == (
        ProgressionState.initial(RiskMode.ANTI_MARTINGALE, D("0.01")),
        2,
    )
    with session_factory(engine).begin() as s:
        event = s.scalar(select(AuditEvent).where(AuditEvent.event_type == "PROGRESSION_RESET"))
        assert event is not None
        assert event.payload == {
            "scope_key": KEY,
            "symbol": "XAUUSD",
            "old_theoretical_lot": theoretical,
            "new_theoretical_lot": "0.01",
            "broker_volume_max": "100",
        }


def test_invalid_signal_is_recorded_with_reason(engine: Engine, gw: FakeGateway) -> None:
    out = send(orchestrator(engine, gw), text="GOLD BUY NOW\nTP 4510")
    assert (out.state, out.reason) == (SignalState.REJECTED_INVALID_SIGNAL, "STOP_LOSS_MISSING")
    with session_factory(engine).begin() as s:
        n = s.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.SIGNAL_REJECTED)
        )
    assert n == 1
    assert gw.orders == []


def test_unknown_source_rejected(engine: Engine, gw: FakeGateway) -> None:
    out = send(orchestrator(engine, gw), source="stranger")
    assert (out.state, out.reason) == (SignalState.REJECTED_INVALID_SIGNAL, "UNKNOWN_SOURCE")


def test_paper_mode_sizes_but_never_sends(engine: Engine, gw: FakeGateway) -> None:
    out = send(orchestrator(engine, gw, execution_mode=ExecutionMode.PAPER))
    assert out.state is SignalState.SKIPPED_PAPER
    assert out.executed_lot == D("0.01")
    assert gw.orders == []
    assert trades(engine) == []


def test_mode_change_resets_scope_and_is_audited(engine: Engine, gw: FakeGateway) -> None:
    send(orchestrator(engine, gw))
    with session_factory(engine).begin() as s:
        repo.save_progression(
            s,
            KEY,
            ProgressionState(
                mode=RiskMode.ANTI_MARTINGALE, base_lot=D("0.01"), theoretical_lot=D("0.04")
            ),
            expected_version=1,
        )
        controls.change_risk_mode(s, RiskMode.DOUBLE_EVERY_FIVE, actor="42")
    state, _ = progression(engine) or (None, 0)
    assert state == ProgressionState.initial(RiskMode.DOUBLE_EVERY_FIVE, D("0.01"))
    with session_factory(engine).begin() as s:
        event = s.scalar(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.MODE_CHANGED)
        )
    assert event is not None
    assert event.actor == "42"
    assert event.payload["scopes"][0]["old_theoretical_lot"] == "0.04"
