"""Bounded retry/backoff (§57, §58) and MT5 connection tracking with audited transitions."""

import logging
import os
import sys
import time
from collections.abc import Callable
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from plough_backer.enums import AuditEventType
from plough_backer.exceptions import BrokerError
from plough_backer.persistence import repositories as repo

log = logging.getLogger(__name__)


def _system_memory_percent() -> float:
    """Physical memory usage without an extra runtime dependency."""
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return float(status.memory_load)

    page_size = os.sysconf("SC_PAGE_SIZE")
    total_pages = os.sysconf("SC_PHYS_PAGES")
    available_pages = os.sysconf("SC_AVPHYS_PAGES")
    return (1 - available_pages / total_pages) * 100 if page_size and total_pages else 0.0


class MemoryPressureMonitor:
    """Alert once as RAM crosses 75/85/95%, then re-arm after recovery below 75%."""

    def __init__(self, read_percent: Callable[[], float] = _system_memory_percent) -> None:
        self._read_percent = read_percent
        self._level = 0

    @staticmethod
    def _at(percent: float) -> int:
        if percent >= 95:
            return 3
        if percent >= 85:
            return 2
        if percent >= 75:
            return 1
        return 0

    def sample(self) -> str | None:
        percent = self._read_percent()
        level = self._at(percent)
        previous = self._level
        self._level = level
        if level > previous:
            threshold = (75, 85, 95)[level - 1]
            label = ("WARNING", "HIGH", "CRITICAL")[level - 1]
            return f"{label}: VPS memory reached {percent:.1f}% (threshold {threshold}%)."
        if level == 0 and previous > 0:
            return f"VPS memory recovered to {percent:.1f}% (below 75%)."
        return None


def backoff_delays(attempts: int, base: float, cap: float) -> list[float]:
    """Exponential delays between attempts, capped: base, 2*base, 4*base ... <= cap."""
    return [min(cap, base * 2**i) for i in range(max(attempts - 1, 0))]


def retry[T](
    fn: Callable[[], T],
    *,
    attempts: int,
    base: float,
    cap: float,
    retry_on: tuple[type[Exception], ...],
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call fn up to `attempts` times. Bounded: re-raises the last error, never loops forever."""
    delays = backoff_delays(attempts, base, cap)
    for delay in [*delays, None]:
        try:
            return fn()
        except retry_on as exc:
            if delay is None:
                raise
            log.warning("retrying", extra={"error": str(exc), "delay_seconds": delay})
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class Connectable(Protocol):
    def connect(self) -> None: ...
    def is_connected(self) -> bool: ...


class MT5Monitor:
    """Keeps MT5 connected with bounded backoff; audits every connected/disconnected edge."""

    def __init__(
        self,
        client: Connectable,
        sessions: sessionmaker[Session],
        *,
        attempts: int = 5,
        base: float = 1.0,
        cap: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client, self._sessions = client, sessions
        self._attempts, self._base, self._cap, self._sleep = attempts, base, cap, sleep
        self.connected: bool | None = None  # unknown until first check

    def _record(self, connected: bool, detail: str = "") -> None:
        if connected == self.connected:
            return
        self.connected = connected
        event = AuditEventType.MT5_CONNECTED if connected else AuditEventType.MT5_DISCONNECTED
        with self._sessions.begin() as s:
            repo.append_audit(s, event, detail=detail)
        log.log(logging.INFO if connected else logging.ERROR, event.lower())

    def ensure(self) -> bool:
        """True if connected after (at most) one bounded reconnect cycle."""
        if self._client.is_connected():
            self._record(True)
            return True
        self._record(False, "not connected")
        try:
            retry(
                self._client.connect,
                attempts=self._attempts,
                base=self._base,
                cap=self._cap,
                retry_on=(BrokerError,),
                sleep=self._sleep,
            )
        except BrokerError as exc:
            log.error("mt5_reconnect_failed", extra={"error": str(exc)})
            return False
        self._record(True, "reconnected")
        return True
