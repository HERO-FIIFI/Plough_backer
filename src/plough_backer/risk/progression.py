"""The six plough-back methods (README §15) as pure functions on theoretical lot.

`settle` is called only for a SETTLED executed trade (INV-02). Blocked, rejected, paused
and skipped signals never reach it (INV-03/04/05). Broker volume normalization happens
elsewhere and never writes back here (INV-06); the explicit broker-limit safety reset is
a separate progression lifecycle event.

Each state carries its own `base_lot`, so the rules are the same whether a scope's base is a
symbol's volume_min or a unit multiplier (SPEC_NOTES Q-01 is decided by the caller).
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal, Inexact, localcontext
from typing import Self

from plough_backer.enums import RiskMode, TradeOutcome

# Q-10: 50 significant digits, and any rounding raises instead of silently altering the
# progression. Mode 6 stays exact for 42 consecutive wins (0.01 x 1.5^42 ~ 250,000 lots).
PRECISION = 50
FIVE_TRADE_BLOCK = 5
TWO = Decimal(2)
ONE_AND_HALF = Decimal("1.5")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProgressionState:
    """README §52 minus persistence fields (scope_id, version, updated_at live in the DB)."""

    mode: RiskMode
    base_lot: Decimal
    theoretical_lot: Decimal
    wins: int = 0
    losses: int = 0
    mode5_block_trade_count: int = 0  # trades settled in the current Mode 5 block, 0..4

    def __post_init__(self) -> None:
        for lot in (self.base_lot, self.theoretical_lot):
            if not isinstance(lot, Decimal) or not lot.is_finite():
                raise TypeError("lots must be finite Decimal (INV-07)")
        if self.base_lot <= 0:
            raise ValueError("base_lot must be positive")
        if self.theoretical_lot < self.base_lot:
            raise ValueError("theoretical_lot cannot be below base_lot")
        if min(self.wins, self.losses) < 0:
            raise ValueError("counters cannot be negative")
        if not 0 <= self.mode5_block_trade_count < FIVE_TRADE_BLOCK:
            raise ValueError("mode5_block_trade_count must be in 0..4")

    @classmethod
    def initial(cls, mode: RiskMode, base_lot: Decimal) -> Self:
        """Fresh state at base lot — also used on a mode change (README §33)."""
        return cls(mode=mode, base_lot=base_lot, theoretical_lot=base_lot)

    @property
    def completed_trades(self) -> int:
        return self.wins + self.losses


# Each rule: (state, won) -> (next theoretical lot, next Mode 5 block count)
_Rule = Callable[[ProgressionState, bool], tuple[Decimal, int]]


def _anti_martingale(s: ProgressionState, won: bool) -> tuple[Decimal, int]:
    return (s.theoretical_lot * TWO if won else s.base_lot), 0


def _martingale(s: ProgressionState, won: bool) -> tuple[Decimal, int]:
    return (s.base_lot if won else s.theoretical_lot * TWO), 0


def _win_double_loss_half(s: ProgressionState, won: bool) -> tuple[Decimal, int]:
    if won:
        return s.theoretical_lot * TWO, 0
    return max(s.theoretical_lot / TWO, s.base_lot), 0


def _always_double(s: ProgressionState, won: bool) -> tuple[Decimal, int]:
    return s.theoretical_lot * TWO, 0


def _double_every_five(s: ProgressionState, won: bool) -> tuple[Decimal, int]:
    count = s.mode5_block_trade_count + 1
    if count == FIVE_TRADE_BLOCK:
        return s.theoretical_lot * TWO, 0
    return s.theoretical_lot, count


def _win_half_increment_loss_hold(s: ProgressionState, won: bool) -> tuple[Decimal, int]:
    return (s.theoretical_lot * ONE_AND_HALF if won else s.theoretical_lot), 0


RULES: dict[RiskMode, _Rule] = {
    RiskMode.ANTI_MARTINGALE: _anti_martingale,
    RiskMode.MARTINGALE: _martingale,
    RiskMode.WIN_DOUBLE_LOSS_HALF: _win_double_loss_half,
    RiskMode.ALWAYS_DOUBLE: _always_double,
    RiskMode.DOUBLE_EVERY_FIVE: _double_every_five,
    RiskMode.WIN_HALF_INCREMENT_LOSS_HOLD: _win_half_increment_loss_hold,
}


def settle(state: ProgressionState, outcome: TradeOutcome) -> ProgressionState:
    """Next state after one settled trade. Pure; the input state is never modified."""
    if outcome not in (TradeOutcome.WIN, TradeOutcome.LOSS):
        # §22 V1 default: BREAKEVEN unchanged, OTHER unchanged (+ review flag, set by the
        # settlement layer). Applies to Modes 4 and 5 too — SPEC_NOTES Q-02.
        return state
    won = outcome is TradeOutcome.WIN
    with localcontext() as ctx:
        ctx.prec = PRECISION
        ctx.traps[Inexact] = True  # raise, never round the theoretical lot
        lot, block_count = RULES[state.mode](state, won)
    return replace(
        state,
        theoretical_lot=lot,
        mode5_block_trade_count=block_count,
        wins=state.wins + won,
        losses=state.losses + (not won),
    )


def reset_at_broker_limit(
    state: ProgressionState, *, volume_min: Decimal, volume_max: Decimal
) -> tuple[ProgressionState, bool]:
    """Reset before an at/above-max progression lot can be traded."""
    if state.theoretical_lot < volume_max:
        return state, False
    return ProgressionState.initial(state.mode, volume_min), True
