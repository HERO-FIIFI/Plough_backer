"""Process-isolated MT5 gateway.

The MetaTrader5 Python package owns process-global terminal state. Each account therefore
gets one child process and one terminal. The coordinator communicates over a local Pipe and
never imports or initializes MetaTrader5 itself.
"""

import multiprocessing
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from multiprocessing.connection import Connection
from typing import Any, cast

from pydantic import SecretStr

from plough_backer.enums import Direction
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


@dataclass(frozen=True, slots=True)
class MT5WorkerSpec:
    account_id: str
    login: int
    password: str
    server: str
    deviation_points: int
    magic: int
    terminal_path: str | None = None


def _error_payload(exc: Exception) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "retcode": exc.retcode if isinstance(exc, BrokerRejected) else None,
    }


def serve_mt5_worker(connection: Connection, spec: MT5WorkerSpec) -> None:
    """Child-process entry point. It must remain module-level for Windows spawn."""
    from plough_backer.trading.mt5_client import MT5Client, load_mt5

    gateway = MT5Client(
        load_mt5(),
        login=spec.login,
        password=SecretStr(spec.password),
        server=spec.server,
        deviation_points=spec.deviation_points,
        magic=spec.magic,
        terminal_path=spec.terminal_path,
    )
    try:
        gateway.connect()
        while True:
            request = connection.recv()
            if request["method"] == "shutdown":
                connection.send({"id": request["id"], "ok": True, "value": None})
                return
            try:
                value = getattr(gateway, request["method"])(
                    *request["args"], **request["kwargs"]
                )
                connection.send({"id": request["id"], "ok": True, "value": value})
            except Exception as exc:
                connection.send(
                    {"id": request["id"], "ok": False, "error": _error_payload(exc)}
                )
    except (EOFError, BrokenPipeError):
        return
    finally:
        gateway.shutdown()
        connection.close()


_WorkerTarget = Callable[[Connection, MT5WorkerSpec], None]


class ProcessBrokerGateway:
    """BrokerGateway proxy backed by one dedicated child process."""

    def __init__(
        self,
        spec: MT5WorkerSpec,
        *,
        timeout_seconds: float = 15.0,
        worker_target: _WorkerTarget = serve_mt5_worker,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self._connection = parent
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._process = context.Process(
            target=worker_target,
            args=(child, spec),
            name=f"mt5-{spec.account_id}",
            daemon=True,
        )
        self._process.start()
        child.close()

    @property
    def alive(self) -> bool:
        return self._process.is_alive()

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            if not self.alive:
                raise MT5Unavailable(f"MT5 worker {self._process.name} is not running")
            request_id = uuid.uuid4().hex
            try:
                self._connection.send(
                    {"id": request_id, "method": method, "args": args, "kwargs": kwargs}
                )
                if not self._connection.poll(self._timeout):
                    raise MT5Unavailable(
                        f"MT5 worker {self._process.name} timed out during {method}"
                    )
                response = self._connection.recv()
            except (EOFError, BrokenPipeError, OSError) as exc:
                raise MT5Unavailable(
                    f"MT5 worker {self._process.name} disconnected during {method}"
                ) from exc
            if response.get("id") != request_id:
                raise ExecutionFailed("MT5 worker returned a mismatched response")
            if response.get("ok"):
                return response.get("value")
            self._raise_remote(response["error"])

    @staticmethod
    def _raise_remote(error: dict[str, Any]) -> None:
        error_type = str(error.get("type", ""))
        message = str(error.get("message", "MT5 worker error"))
        if error_type == "BrokerRejected":
            raise BrokerRejected(int(error["retcode"]), message)
        exception_type = {
            "MT5Unavailable": MT5Unavailable,
            "SymbolUnavailable": SymbolUnavailable,
            "ExecutionFailed": ExecutionFailed,
        }.get(error_type, ExecutionFailed)
        raise exception_type(message)

    def is_connected(self) -> bool:
        try:
            return bool(self._call("is_connected"))
        except MT5Unavailable:
            return False

    def connect(self) -> None:
        self._call("connect")

    def account_snapshot(self) -> AccountSnapshot:
        return cast(AccountSnapshot, self._call("account_snapshot"))

    def symbol_specification(self, symbol: str) -> SymbolSpecification:
        return cast(SymbolSpecification, self._call("symbol_specification", symbol))

    def calc_profit(
        self,
        symbol: str,
        direction: Direction,
        volume: Decimal,
        price_open: Decimal,
        price_close: Decimal,
    ) -> Decimal:
        return cast(
            Decimal,
            self._call("calc_profit", symbol, direction, volume, price_open, price_close),
        )

    def current_tick(self, symbol: str) -> Tick:
        return cast(Tick, self._call("current_tick", symbol))

    def submit_order(self, request: OrderRequest) -> OrderResult:
        return cast(OrderResult, self._call("submit_order", request))

    def open_position_ids(self) -> set[int]:
        return cast(set[int], self._call("open_position_ids"))

    def deals_for_position(self, position_id: int) -> list[Deal]:
        return cast(list[Deal], self._call("deals_for_position", position_id))

    def shutdown(self) -> None:
        if self.alive:
            with suppress(MT5Unavailable):
                self._call("shutdown")
            self._process.join(timeout=5)
        if self.alive:
            self._process.terminate()
            self._process.join(timeout=2)
        self._connection.close()

    def __enter__(self) -> "ProcessBrokerGateway":
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()
