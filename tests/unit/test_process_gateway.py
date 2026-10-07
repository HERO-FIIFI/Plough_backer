"""The MT5 proxy owns a child process and preserves broker boundary semantics."""

from multiprocessing.connection import Connection

import pytest

from plough_backer.exceptions import MT5Unavailable
from plough_backer.trading.gateway import AccountSnapshot
from plough_backer.trading.process_gateway import MT5WorkerSpec, ProcessBrokerGateway
from tests.fakes import D


def fake_worker(connection: Connection, _: MT5WorkerSpec) -> None:
    while True:
        request = connection.recv()
        method = request["method"]
        if method == "shutdown":
            connection.send({"id": request["id"], "ok": True, "value": None})
            return
        if method == "is_connected":
            value = True
        elif method == "account_snapshot":
            value = AccountSnapshot(
                balance=D(50),
                equity=D(50),
                margin=D(0),
                free_margin=D(50),
                currency="USD",
            )
        else:
            connection.send(
                {
                    "id": request["id"],
                    "ok": False,
                    "error": {"type": "MT5Unavailable", "message": "offline"},
                }
            )
            continue
        connection.send({"id": request["id"], "ok": True, "value": value})


def worker_spec() -> MT5WorkerSpec:
    return MT5WorkerSpec(
        account_id="method-1",
        login=123,
        password="secret",
        server="Demo",
        deviation_points=10,
        magic=1001,
    )


def test_process_gateway_round_trip_and_clean_shutdown() -> None:
    gateway = ProcessBrokerGateway(worker_spec(), worker_target=fake_worker)
    try:
        assert gateway.is_connected()
        assert gateway.account_snapshot().balance == D(50)
        with pytest.raises(MT5Unavailable, match="offline"):
            gateway.current_tick("XAUUSD")
    finally:
        gateway.shutdown()
    assert not gateway.alive
