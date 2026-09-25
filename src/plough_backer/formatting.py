"""Display formatting shared by Telegram messages and reports. Presentation only."""

from decimal import Decimal

from plough_backer.enums import RiskMode

_EMOJI_NUMBERS = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣"}


def money(value: Decimal | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def lot(value: Decimal | None) -> str:
    """At least 4 decimals like the README (0.0400); more when Deriv-size lots need them."""
    if value is None:
        return "—"
    exponent = value.normalize().as_tuple().exponent
    places = max(4, -exponent if isinstance(exponent, int) else 4)
    return f"{value:.{places}f}"


def mode_label(mode: RiskMode) -> str:
    return f"{_EMOJI_NUMBERS[int(mode)]} {mode.label}"
