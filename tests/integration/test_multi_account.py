"""One Telegram intake fans out safely to independently journaled MT5 accounts."""

from datetime import UTC, datetime

from sqlalchemy import Engine, select

from plough_backer.config import SourcesConfig, SymbolsConfig
from plough_backer.enums import (
    ExecutionMode,
    ProgressionScope,
    RiskEntrySource,
    RiskMode,
    SignalState,
    VolumeMaxPolicy,
)
from plough_backer.persistence.database import session_factory
from plough_backer.persistence.models import Trade
from plough_backer.trading.executor import (
    ExecutionPolicy,
    MultiAccountOrchestrator,
    Orchestrator,
    SignalIngestor,
)
from tests.fakes import FakeGateway

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
MESSAGE = "BUY GOLD 4500\nSL 4492\nTP1 4506\nTP2 4512\nTP3 4518"
SYMBOLS = SymbolsConfig.model_validate(
    {
        "symbols": {
            "GOLD": {
                "aliases": ["GOLD", "XAUUSD"],
                "mt5_symbol": "XAUUSD",
                "asset_class": "GOLD",
            }
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
POLICY = ExecutionPolicy(
    execution_mode=ExecutionMode.DEMO,
    progression_scope=ProgressionScope.PER_SOURCE,
    volume_max_policy=VolumeMaxPolicy.REJECT,
    risk_entry=RiskEntrySource.SIGNAL_ENTRY_OR_LIVE,
)


def system(
    engine: Engine, gateways: dict[str, FakeGateway], modes: dict[str, RiskMode]
) -> MultiAccountOrchestrator:
    sessions = session_factory(engine)
    return MultiAccountOrchestrator(
        ingestor=SignalIngestor(sessions=sessions, symbols=SYMBOLS, sources=SOURCES),
        executors={
            account_id: Orchestrator(
                sessions=sessions,
                gateway=gateway,
                symbols=SYMBOLS,
                sources=SOURCES,
                policy=POLICY,
                account_id=account_id,
                risk_mode=modes[account_id],
                clock=lambda: NOW,
            )
            for account_id, gateway in gateways.items()
        },
    )


def send(orchestrator: MultiAccountOrchestrator) -> object:
    return orchestrator.process_message(
        source_id="gold",
        message_id=1,
        message_time=NOW,
        text=MESSAGE,
        received_at=NOW,
    )


def test_signal_executes_once_in_every_attached_account(engine: Engine) -> None:
    gateways = {"method-1-a": FakeGateway(), "method-1-b": FakeGateway()}
    modes = {account_id: RiskMode.ANTI_MARTINGALE for account_id in gateways}
    app = system(engine, gateways, modes)

    result = send(app)

    assert {account_id: outcome.state for account_id, outcome in result.accounts.items()} == {
        "method-1-a": SignalState.OPEN,
        "method-1-b": SignalState.OPEN,
    }
    assert all(len(gateway.orders) == 1 for gateway in gateways.values())
    with session_factory(engine).begin() as session:
        trades = list(session.scalars(select(Trade).order_by(Trade.account_id)))
    assert [trade.account_id for trade in trades] == ["method-1-a", "method-1-b"]
    assert [trade.risk_mode for trade in trades] == [1, 1]

    duplicate = send(app)
    assert duplicate.intake is not None
    assert duplicate.intake.state is SignalState.REJECTED_DUPLICATE
    assert all(len(gateway.orders) == 1 for gateway in gateways.values())


def test_one_account_failure_does_not_block_other_accounts(engine: Engine) -> None:
    broken = FakeGateway()

    def explode(_: str) -> object:
        raise RuntimeError("terminal crashed")

    broken.symbol_specification = explode  # type: ignore[method-assign]
    healthy = FakeGateway()
    app = system(
        engine,
        {"broken": broken, "healthy": healthy},
        {"broken": RiskMode.MARTINGALE, "healthy": RiskMode.WIN_DOUBLE_LOSS_HALF},
    )

    result = send(app)

    assert result.accounts["broken"].state is SignalState.EXECUTION_FAILED
    assert result.accounts["healthy"].state is SignalState.OPEN
    assert len(healthy.orders) == 1
    with session_factory(engine).begin() as session:
        (trade,) = session.scalars(select(Trade)).all()
    assert (trade.account_id, trade.risk_mode) == ("healthy", 3)


def test_account_can_be_activated_after_credentials_are_saved(engine: Engine) -> None:
    sessions = session_factory(engine)
    app = MultiAccountOrchestrator(
        ingestor=SignalIngestor(sessions=sessions, symbols=SYMBOLS, sources=SOURCES),
        executors={
            "method-1": Orchestrator(
                sessions=sessions,
                gateway=None,
                symbols=SYMBOLS,
                sources=SOURCES,
                policy=POLICY,
                account_id="method-1",
                risk_mode=RiskMode.ANTI_MARTINGALE,
            )
        },
    )
    unavailable = send(app)
    assert unavailable.accounts["method-1"].state is SignalState.SKIPPED_MT5_UNAVAILABLE

    gateway = FakeGateway()
    app.replace_executor(
        "method-1",
        Orchestrator(
            sessions=sessions,
            gateway=gateway,
            symbols=SYMBOLS,
            sources=SOURCES,
            policy=POLICY,
            account_id="method-1",
            risk_mode=RiskMode.ANTI_MARTINGALE,
        ),
    )
    activated = app.process_message(
        source_id="gold",
        message_id=2,
        message_time=NOW,
        text=MESSAGE,
        received_at=NOW,
    )
    assert activated.accounts["method-1"].state is SignalState.OPEN
    assert len(gateway.orders) == 1
