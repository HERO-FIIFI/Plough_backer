"""Closed vocabularies from the README. Values are persisted — never rename a member."""

from enum import IntEnum, StrEnum


class ExecutionMode(StrEnum):
    """README §6. LIVE is never inferred; it must be configured explicitly."""

    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE = "LIVE"


class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """README §8."""

    MARKET = "MARKET"
    BUY_LIMIT = "BUY_LIMIT"
    SELL_LIMIT = "SELL_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_STOP = "SELL_STOP"

    @property
    def is_pending(self) -> bool:
        return self is not OrderType.MARKET

    @property
    def implied_direction(self) -> Direction | None:
        """Pending types encode a direction; MARKET does not."""
        if self is OrderType.MARKET:
            return None
        return Direction.BUY if self.value.startswith("BUY") else Direction.SELL


class SignalState(StrEnum):
    """README §7 lifecycle, plus the terminal states named in §7, §34 and §57.

    Allowed transitions are not encoded yet — see docs/SPEC_NOTES.md (Q-11).
    """

    # Happy path, in order
    RECEIVED = "RECEIVED"
    SOURCE_VALIDATED = "SOURCE_VALIDATED"
    PARSED = "PARSED"
    NORMALIZED = "NORMALIZED"
    SYMBOL_RESOLVED = "SYMBOL_RESOLVED"
    VALIDATED = "VALIDATED"
    DEDUPLICATED = "DEDUPLICATED"
    SIZED = "SIZED"
    EQUITY_LOCK_CHECKED = "EQUITY_LOCK_CHECKED"
    EXECUTION_REQUESTED = "EXECUTION_REQUESTED"
    EXECUTED = "EXECUTED"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    SETTLED = "SETTLED"

    # Terminal (§7)
    REJECTED_PARSE = "REJECTED_PARSE"
    REJECTED_INVALID_SIGNAL = "REJECTED_INVALID_SIGNAL"
    REJECTED_DUPLICATE = "REJECTED_DUPLICATE"
    BLOCKED_EQUITY_LOCK = "BLOCKED_EQUITY_LOCK"
    BROKER_REJECTED = "BROKER_REJECTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    CANCELLED = "CANCELLED"
    CLOSED_WIN = "CLOSED_WIN"
    CLOSED_LOSS = "CLOSED_LOSS"
    CLOSED_OTHER = "CLOSED_OTHER"
    # Terminal (§34, §57)
    SKIPPED_PAUSED = "SKIPPED_PAUSED"
    SKIPPED_MT5_UNAVAILABLE = "SKIPPED_MT5_UNAVAILABLE"
    # Provisional, not named in the README (Q-11): kill switch active / PAPER mode sizing only
    SKIPPED_STOPPED = "SKIPPED_STOPPED"
    SKIPPED_PAPER = "SKIPPED_PAPER"


class TradingStatus(StrEnum):
    """§34 PAUSE / §35 STOP. Both mean 'no new MT5 execution'; positions untouched."""

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class RiskEntrySource(StrEnum):
    """Which price the pre-trade risk uses for MARKET orders (Q-06). No default."""

    LIVE_PRICE = "LIVE_PRICE"  # ask for BUY, bid for SELL
    SIGNAL_ENTRY_OR_LIVE = "SIGNAL_ENTRY_OR_LIVE"  # signal entry; live only when absent (NOW)


class RejectionReason(StrEnum):
    """Machine-readable reasons for REJECTED_* states. STOP_LOSS_MISSING is from §10."""

    STOP_LOSS_MISSING = "STOP_LOSS_MISSING"
    TP2_MISSING = "TP2_MISSING"  # §12 missing_tp2_policy=REJECT
    MALFORMED_NUMBER = "MALFORMED_NUMBER"  # §62
    UNSUPPORTED_SYMBOL = "UNSUPPORTED_SYMBOL"  # §62
    UNKNOWN_SOURCE = "UNKNOWN_SOURCE"  # §7 SOURCE_VALIDATED
    UNRECOGNIZED_FORMAT = "UNRECOGNIZED_FORMAT"
    AMBIGUOUS_SIGNAL = "AMBIGUOUS_SIGNAL"  # e.g. two directions/symbols/SLs (Q-19)
    AMBIGUOUS_ENTRY = "AMBIGUOUS_ENTRY"  # e.g. entry range "4500-4495" (Q-14)
    ENTRY_MISSING = "ENTRY_MISSING"  # pending order without a price
    TP_POLICY_NOT_CONFIGURED = "TP_POLICY_NOT_CONFIGURED"  # non-Gold TP rule unset (Q-04)
    TP_MISSING = "TP_MISSING"  # configured TPn absent


class TradeOutcome(StrEnum):
    """README §22 minimum classifications."""

    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"
    OTHER = "OTHER"


class RiskMode(IntEnum):
    """README §15. Integer values are the user-facing mode numbers."""

    ANTI_MARTINGALE = 1
    MARTINGALE = 2
    WIN_DOUBLE_LOSS_HALF = 3
    ALWAYS_DOUBLE = 4
    DOUBLE_EVERY_FIVE = 5
    WIN_HALF_INCREMENT_LOSS_HOLD = 6

    @property
    def label(self) -> str:
        return _RISK_MODE_LABELS[self]


# Labels as shown in the §32 selector.
_RISK_MODE_LABELS = {
    RiskMode.ANTI_MARTINGALE: "Anti-Martingale",
    RiskMode.MARTINGALE: "Martingale",
    RiskMode.WIN_DOUBLE_LOSS_HALF: "Win ×2 / Loss ÷2",
    RiskMode.ALWAYS_DOUBLE: "Always ×2",
    RiskMode.DOUBLE_EVERY_FIVE: "×2 Every 5 Trades",
    RiskMode.WIN_HALF_INCREMENT_LOSS_HOLD: "Win +50% / Loss Hold",
}


class ProgressionScope(StrEnum):
    """README §23."""

    GLOBAL = "GLOBAL"
    PER_SOURCE = "PER_SOURCE"


class AssetClass(StrEnum):
    """Drives Gold TP policy (§12). Never used to assume contract specs (§13)."""

    GOLD = "GOLD"
    SYNTHETIC = "SYNTHETIC"
    FOREX_CFD = "FOREX_CFD"


class TakeProfitPolicy(StrEnum):
    """§12: Gold uses TP2. Non-Gold symbols use whatever the owner configures per symbol;
    unset means the signal is rejected (Q-04) — the code never picks one."""

    TP1 = "TP1"
    TP2 = "TP2"
    TP3 = "TP3"

    @property
    def tp_number(self) -> int:
        return int(self.value[2:])


class MissingTp2Policy(StrEnum):
    """§12. Only REJECT is specified; any fallback needs a spec decision (Q-04)."""

    REJECT = "REJECT"


class VolumeMaxPolicy(StrEnum):
    """What to do when the normalized lot exceeds broker volume_max (§17). No default: Q-05."""

    CAP = "CAP"  # execute at the largest valid volume, flagged as capped
    REJECT = "REJECT"  # broker constraint failure (§21); progression untouched


class ShadowStatus(StrEnum):
    """README §38."""

    ACTIVE = "ACTIVE"
    SHADOW_FAILED = "SHADOW_FAILED"


class AuditEventType(StrEnum):
    """README §43."""

    APP_STARTED = "APP_STARTED"
    APP_STOPPED = "APP_STOPPED"
    MT5_CONNECTED = "MT5_CONNECTED"
    MT5_DISCONNECTED = "MT5_DISCONNECTED"
    TELEGRAM_CONNECTED = "TELEGRAM_CONNECTED"
    SIGNAL_RECEIVED = "SIGNAL_RECEIVED"
    SIGNAL_PARSED = "SIGNAL_PARSED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    SIGNAL_DUPLICATE = "SIGNAL_DUPLICATE"
    TRADE_REQUESTED = "TRADE_REQUESTED"
    TRADE_EXECUTED = "TRADE_EXECUTED"
    TRADE_REJECTED = "TRADE_REJECTED"
    TRADE_SETTLED = "TRADE_SETTLED"
    MODE_CHANGED = "MODE_CHANGED"
    EQUITY_LOCK_CHANGED = "EQUITY_LOCK_CHANGED"
    PAUSED = "PAUSED"
    RESUMED = "RESUMED"
    KILL_SWITCH = "KILL_SWITCH"
    REPORT_GENERATED = "REPORT_GENERATED"
