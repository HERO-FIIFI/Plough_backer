"""Theoretical -> executable lot (README §16, §17). The single place volumes are rounded.

The theoretical lot is only read, never changed (INV-06).
"""

from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext

from plough_backer.enums import VolumeMaxPolicy
from plough_backer.exceptions import InvalidSymbolSpecification, VolumeAboveMaximum

_ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class VolumeDecision:
    theoretical: Decimal  # unchanged input, for the journal
    executable: Decimal  # what is sent to MT5
    capped_at_max: bool  # True only under VolumeMaxPolicy.CAP — must be logged (§17)


def normalize_volume(
    theoretical_volume: Decimal,
    volume_min: Decimal,
    volume_max: Decimal,
    volume_step: Decimal,
    *,
    max_policy: VolumeMaxPolicy,
) -> VolumeDecision:
    """Round to the nearest broker step (§17 default), never below volume_min.

    Ties round up: the §16 table maps 0.015 -> 0.02.
    """
    values = (theoretical_volume, volume_min, volume_max, volume_step)
    if not all(isinstance(v, Decimal) and v.is_finite() for v in values):
        raise TypeError("volumes must be finite Decimal (INV-07)")
    if volume_step <= 0 or volume_min <= 0 or volume_max < volume_min:
        raise InvalidSymbolSpecification(
            f"bad volume spec min={volume_min} max={volume_max} step={volume_step}"
        )
    if volume_min % volume_step != 0:
        # Grid anchoring is undefined by the spec (Q-05); fail closed rather than guess.
        raise InvalidSymbolSpecification(f"volume_min {volume_min} is not a multiple of step")

    with localcontext() as ctx:
        ctx.prec = 100  # theoretical lots can carry up to 50 significant digits
        steps = (theoretical_volume / volume_step).quantize(_ONE, rounding=ROUND_HALF_UP)
        executable = max(steps * volume_step, volume_min)
        if executable <= volume_max:
            return VolumeDecision(theoretical_volume, executable, capped_at_max=False)
        if max_policy is VolumeMaxPolicy.REJECT:
            raise VolumeAboveMaximum(
                f"volume {executable} (theoretical {theoretical_volume}) > max {volume_max}"
            )
        capped = (volume_max / volume_step).to_integral_value(ROUND_FLOOR) * volume_step
    return VolumeDecision(theoretical_volume, capped, capped_at_max=True)
