"""Decimal handling (README §16, INV-07).

Every price, lot and money value crosses into the domain through `to_decimal`.
MT5 returns binary floats; those are converted via their shortest round-trip repr, so
0.01 becomes Decimal("0.01") and not Decimal("0.01000000000000000020816681711721685...").
"""

from decimal import Decimal, InvalidOperation
from typing import Final

from plough_backer.exceptions import PloughBackerError

# ponytail: progression precision is not fixed here. Mode 6 (×1.5) turns base×15^n/10^n, so
# the default 28-digit context stays exact for 23 consecutive wins, then rounds. Q-10, Phase 1.
ZERO: Final = Decimal(0)


class DecimalConversionError(PloughBackerError, ValueError):
    """A value cannot be represented as a finite Decimal."""


def to_decimal(value: Decimal | int | str | float) -> Decimal:
    """Convert to a finite Decimal. Floats go through repr() to avoid binary noise."""
    if isinstance(value, bool):  # bool is an int subclass; True -> 1 is never intended
        raise DecimalConversionError(f"refusing to convert bool {value!r}")
    try:
        if isinstance(value, float):
            result = Decimal(repr(value))
        elif isinstance(value, str):
            result = Decimal(value.strip())
        else:
            result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise DecimalConversionError(f"not a number: {value!r}") from exc
    if not result.is_finite():
        raise DecimalConversionError(f"not finite: {value!r}")
    return result


def decimal_to_str(value: Decimal) -> str:
    """Canonical, exponent-free storage/display string. Preserves all significant digits.

    Trailing zeros are kept (Decimal("0.010000") -> "0.010000") so round-trips are exact.
    """
    if not value.is_finite():
        raise DecimalConversionError(f"not finite: {value!r}")
    return format(value, "f")
