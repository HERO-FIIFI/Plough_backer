"""Bounded retry/backoff (§57, §58) and MT5 connection tracking with audited transitions."""

import logging
import time
from collections.abc import Callable
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from plough_backer.enums import AuditEventType
from plough_backer.exceptions import BrokerError
from plough_backer.persistence import repositories as repo

log = logging.getLogger(__name__)


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
