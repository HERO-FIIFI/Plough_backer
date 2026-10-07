"""Exception hierarchy.

The split matters for trading semantics: a SignalRejected is an invalid instruction (§10),
EquityLockBlocked is the user's safety gate (§20), BrokerError is broker impossibility (§21).
None of them is a trading loss and none may advance progression (INV-03, INV-04, INV-05).
"""

from plough_backer.enums import RejectionReason


class PloughBackerError(Exception):
    """Root of all application errors."""


class ConfigurationError(PloughBackerError):
    """Configuration is missing or invalid. Fail at startup, never mid-trade."""


class SetupTokenInvalid(PloughBackerError):
    """A credential setup token is unknown, expired, or was already consumed."""


class SignalRejected(PloughBackerError):
    """Fail-closed rejection of a Telegram message (README §10)."""

    def __init__(self, reason: RejectionReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else str(reason))


class ParseRejected(SignalRejected):
    """-> REJECTED_PARSE."""


class InvalidSignal(SignalRejected):
    """-> REJECTED_INVALID_SIGNAL."""


class DuplicateSignal(PloughBackerError):
    """-> REJECTED_DUPLICATE (README §24)."""

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        super().__init__(f"duplicate signal fingerprint {fingerprint}")


class EquityLockBlocked(PloughBackerError):
    """-> BLOCKED_EQUITY_LOCK (README §20). Progression must be left untouched."""


class BrokerError(PloughBackerError):
    """MT5/broker-side failure (README §21). Never a trading loss."""


class BrokerRejected(BrokerError):
    """-> BROKER_REJECTED: MT5 returned a non-success retcode."""

    def __init__(self, retcode: int, message: str) -> None:
        self.retcode = retcode
        self.message = message
        super().__init__(f"retcode={retcode}: {message}")


class ExecutionFailed(BrokerError):
    """-> EXECUTION_FAILED: request could not be completed (e.g. connection loss)."""


class MT5Unavailable(BrokerError):
    """-> SKIPPED_MT5_UNAVAILABLE (README §57)."""


class SymbolUnavailable(BrokerError):
    """Symbol cannot be selected/found on this broker (§21)."""


class InvalidSymbolSpecification(BrokerError):
    """Broker symbol spec cannot produce a valid volume (e.g. volume_min off the step grid)."""


class VolumeAboveMaximum(BrokerError):
    """Normalized volume exceeds broker volume_max under VolumeMaxPolicy.REJECT (§17, §21)."""


class PersistenceError(PloughBackerError):
    """Database-level failure."""


class MigrationStateError(PersistenceError):
    """Database schema is not at the Alembic head (README §30 step 2)."""


class ConcurrencyConflict(PersistenceError):
    """Optimistic-lock version mismatch on progression state (README §26, §52)."""
