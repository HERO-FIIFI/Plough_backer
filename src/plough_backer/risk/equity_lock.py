"""Equity Lock (README §20) — the only user-defined gate that may block a valid trade (INV-11).

Pure: it never reads or writes progression, so a block cannot alter it (INV-03).
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class EquityLockDecision:
    allowed: bool
    current_equity: Decimal
    risk_to_sl: Decimal
    projected_equity: Decimal
    protected_equity: Decimal | None  # None when the lock is disabled


def check_equity_lock(
    *,
    enabled: bool,
    protected_equity: Decimal | None,
    current_equity: Decimal,
    risk_to_sl: Decimal,
) -> EquityLockDecision:
    projected = current_equity - risk_to_sl
    if not enabled:
        return EquityLockDecision(True, current_equity, risk_to_sl, projected, None)
    if protected_equity is None:
        raise ValueError("Equity Lock enabled without a protected equity value")
    # §20: blocked when projected < protected. Equal is allowed — provisional, Q-07.
    allowed = projected >= protected_equity
    return EquityLockDecision(allowed, current_equity, risk_to_sl, projected, protected_equity)
