"""Phase 3: restart persistence, concurrency (§66), idempotency (§64), audit immutability."""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError

from plough_backer.enums import AuditEventType, RiskMode, SignalState, TradeOutcome
from plough_backer.exceptions import ConcurrencyConflict, DuplicateSignal
from plough_backer.persistence import repositories as repo
from plough_backer.persistence.database import alembic_config, make_engine, session_factory
from plough_backer.persistence.models import AuditEvent, Base, Signal, SignalEvent, Trade
from plough_backer.risk.progression import ProgressionState, settle

D = Decimal
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'pb.db').as_posix()}"
    cfg = alembic_config(url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    e = make_engine(db_url)
    yield e
    e.dispose()


def restart(engine: Engine, db_url: str) -> Engine:
    """Simulate a process restart: drop every pooled connection, open a fresh engine."""
    engine.dispose()
    return make_engine(db_url)


def test_migrations_match_models(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


# --- Progression --------------------------------------------------------------------------


def test_progression_survives_restart_exactly(engine: Engine, db_url: str) -> None:
    state = ProgressionState.initial(RiskMode.WIN_HALF_INCREMENT_LOSS_HOLD, D("0.0005"))
    for o in (TradeOutcome.WIN, TradeOutcome.WIN, TradeOutcome.LOSS):
        state = settle(state, o)
    with session_factory(engine).begin() as s:
        repo.create_progression(s, "PER_SOURCE:gold:XAUUSD", state)

    engine2 = restart(engine, db_url)
    with session_factory(engine2).begin() as s:
        loaded = repo.load_progression(s, "PER_SOURCE:gold:XAUUSD")
    engine2.dispose()
    assert loaded == (state, 1)
    assert str(loaded[0].theoretical_lot) == "0.001125"  # exact, not a float


def test_stale_version_is_refused(engine: Engine) -> None:
    sessions = session_factory(engine)
    base = ProgressionState.initial(RiskMode.ANTI_MARTINGALE, D("0.01"))
    with sessions.begin() as s:
        repo.create_progression(s, "k", base)
    with sessions.begin() as s:
        repo.save_progression(s, "k", settle(base, TradeOutcome.WIN), expected_version=1)
    with sessions.begin() as s, pytest.raises(ConcurrencyConflict):
        repo.save_progression(s, "k", settle(base, TradeOutcome.WIN), expected_version=1)


def test_concurrent_settlements_lose_no_update(engine: Engine) -> None:
    # §66: many simultaneous read-modify-write cycles; every one must land.
    sessions = session_factory(engine)
    with sessions.begin() as s:
        repo.create_progression(s, "k", ProgressionState.initial(RiskMode.ALWAYS_DOUBLE, D("0.01")))
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with sessions.begin() as s:
                loaded = repo.load_progression(s, "k")
                assert loaded is not None
                state, version = loaded
                next_state = settle(state, TradeOutcome.WIN)
                repo.save_progression(s, "k", next_state, expected_version=version)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with sessions.begin() as s:
        loaded = repo.load_progression(s, "k")
    assert loaded is not None
    state, version = loaded
    assert (state.theoretical_lot, state.wins, version) == (D("0.01") * 2**20, 20, 21)


# --- Signals & executions -----------------------------------------------------------------


def _ingest(s: Any, message_id: int = 1) -> Signal | None:
    return repo.ingest_message(
        s,
        source_id="gold",
        source_message_id=message_id,
        source_timestamp=NOW,
        received_at=NOW,
        raw_message="BUY GOLD 4500\nSL 4492\nTP1 4506\nTP2 4512",
    )


def test_message_ingested_once_even_after_restart(engine: Engine, db_url: str) -> None:
    with session_factory(engine).begin() as s:
        first = _ingest(s)
        assert first is not None
        assert first.signal_id == "PB-000001"
        assert _ingest(s) is None
    engine2 = restart(engine, db_url)
    with session_factory(engine2).begin() as s:
        assert _ingest(s) is None
        second = _ingest(s, message_id=2)
        assert second is not None
        assert second.signal_id == "PB-000002"
        assert s.scalar(select(Signal).where(Signal.source_message_id == 1)) is not None
    engine2.dispose()


def test_transitions_are_persisted(engine: Engine) -> None:
    with session_factory(engine).begin() as s:
        signal = _ingest(s)
        assert signal is not None
        repo.transition_signal(s, signal, SignalState.SOURCE_VALIDATED)
        repo.transition_signal(s, signal, SignalState.REJECTED_INVALID_SIGNAL, "STOP_LOSS_MISSING")
        events = s.scalars(select(SignalEvent.to_state).order_by(SignalEvent.id)).all()
    assert events == ["RECEIVED", "SOURCE_VALIDATED", "REJECTED_INVALID_SIGNAL"]
    assert signal.rejection_reason == "STOP_LOSS_MISSING"


def _trade(signal: Signal, fingerprint: str = "f" * 64) -> Trade:
    zero = D(0)
    return Trade(
        signal_pk=signal.id,
        signal_id=signal.signal_id,
        signal_fingerprint=fingerprint,
        progression_scope_key="k",
        telegram_source_id="gold",
        telegram_message_id=1,
        telegram_timestamp=NOW,
        raw_signal="x",
        parser_version="t",
        symbol_raw="GOLD",
        symbol_mt5="XAUUSD",
        direction="BUY",
        order_type="MARKET",
        stop_loss=D("4492"),
        selected_tp=D("4512"),
        risk_mode=1,
        base_lot=D("0.01"),
        theoretical_lot=D("0.0225"),
        executed_lot=D("0.02"),
        volume_min=D("0.01"),
        volume_max=D("100"),
        volume_step=D("0.01"),
        estimated_risk=D("16"),
        estimated_reward=D("24"),
        balance_before=D(800),
        equity_before=D(800),
        margin_before=zero,
        free_margin_before=D(800),
        equity_lock_enabled=False,
        telegram_received_at=NOW,
    )


def test_fingerprint_executes_once_even_after_restart(engine: Engine, db_url: str) -> None:
    # §64: executed, crash, restart, same message seen again -> ONE execution only.
    with session_factory(engine).begin() as s:
        signal = _ingest(s)
        assert signal is not None
        repo.claim_execution(s, _trade(signal))
    engine2 = restart(engine, db_url)
    with session_factory(engine2).begin() as s:
        signal = s.scalar(select(Signal))
        assert signal is not None
        assert repo.fingerprint_executed(s, "f" * 64)
        with pytest.raises(DuplicateSignal):
            repo.claim_execution(s, _trade(signal))
        assert len(s.scalars(select(Trade)).all()) == 1
    engine2.dispose()


def test_trade_decimals_and_timestamps_round_trip(engine: Engine) -> None:
    with session_factory(engine).begin() as s:
        signal = _ingest(s)
        assert signal is not None
        repo.claim_execution(s, _trade(signal))
    with session_factory(engine).begin() as s:
        t = s.scalar(select(Trade))
        assert t is not None
        assert str(t.theoretical_lot) == "0.0225"
        assert t.telegram_timestamp == NOW
        assert t.telegram_timestamp.tzinfo is UTC


def test_naive_datetime_refused(engine: Engine) -> None:
    with session_factory(engine).begin() as s, pytest.raises(Exception, match="naive"):
        repo.ingest_message(
            s,
            source_id="g",
            source_message_id=9,
            source_timestamp=datetime(2026, 1, 1),  # noqa: DTZ001
            received_at=NOW,
            raw_message="x",
        )


# --- Audit & settings ---------------------------------------------------------------------


def test_audit_events_are_append_only(engine: Engine) -> None:
    with session_factory(engine).begin() as s:
        repo.append_audit(s, AuditEventType.MODE_CHANGED, actor="42", old_theoretical_lot=D("0.04"))
    with session_factory(engine).begin() as s:
        event = s.scalar(select(AuditEvent))
        assert event is not None
        assert event.payload == {"old_theoretical_lot": "0.04"}  # Decimal kept exact
    for sql in ("UPDATE audit_events SET actor = 'x'", "DELETE FROM audit_events"):
        with engine.begin() as conn, pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text(sql))


def test_signal_events_are_append_only(engine: Engine) -> None:
    with session_factory(engine).begin() as s:
        _ingest(s)
    with engine.begin() as conn, pytest.raises(DBAPIError, match="append-only"):
        conn.execute(text("DELETE FROM signal_events"))


def test_env_seed_never_overwrites_persisted_setting(engine: Engine) -> None:
    sessions = session_factory(engine)
    with sessions.begin() as s:
        assert repo.seed_setting(s, "risk_mode", {"mode": 1})
        repo.set_setting(s, "risk_mode", {"mode": 5}, updated_by="telegram:42")
    with sessions.begin() as s:  # next restart re-seeds from env
        assert not repo.seed_setting(s, "risk_mode", {"mode": 1})
        assert repo.get_setting(s, "risk_mode") == {"mode": 5}
