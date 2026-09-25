"""Deterministic SHA-256 signal fingerprint (README §24). Fields: the §24 candidate list (Q-18)."""

import hashlib
from decimal import Decimal

from plough_backer.signals.models import NormalizedSignal


def _canon(value: Decimal | None) -> str:
    # 4500, 4500.0 and 4500.00 are the same price -> same fingerprint.
    return "" if value is None else format(value.normalize(), "f")


def fingerprint(signal: NormalizedSignal) -> str:
    parts = [
        signal.source_id,
        str(signal.source_message_id),
        signal.symbol_mt5 or signal.symbol_raw,
        signal.direction,
        _canon(signal.entry),
        _canon(signal.stop_loss),
        _canon(signal.selected_take_profit),
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
