"""README §15 / §60 progression sequences. Lots listed are the lot USED for each trade."""

from decimal import Decimal
from fractions import Fraction

import pytest

from plough_backer.enums import RiskMode, TradeOutcome
from plough_backer.risk import progression
from plough_backer.risk.progression import ProgressionState, settle

D = Decimal
W, L, BE, OT = TradeOutcome.WIN, TradeOutcome.LOSS, TradeOutcome.BREAKEVEN, TradeOutcome.OTHER


def lots_used(mode: RiskMode, outcomes: str, base: str = "0.01") -> list[Decimal]:
    state = ProgressionState.initial(mode, D(base))
    used = []
    for o in outcomes.split():
        used.append(state.theoretical_lot)
        state = settle(state, TradeOutcome.WIN if o == "W" else TradeOutcome.LOSS)
    used.append(state.theoretical_lot)  # the next lot after the sequence
    return used


def d(*xs: str) -> list[Decimal]:
    return [D(x) for x in xs]


# --- README §60, verbatim -----------------------------------------------------------------


def test_mode1_anti_martingale_readme_sequence() -> None:
    assert lots_used(RiskMode.ANTI_MARTINGALE, "W W L W")[:4] == d("0.01", "0.02", "0.04", "0.01")


def test_mode1_readme_section15_example() -> None:
    assert lots_used(RiskMode.ANTI_MARTINGALE, "W W W L") == d(
        "0.01", "0.02", "0.04", "0.08", "0.01"
    )


def test_mode2_martingale_readme_sequence() -> None:
    assert lots_used(RiskMode.MARTINGALE, "L L W L")[:4] == d("0.01", "0.02", "0.04", "0.01")


def test_mode3_win_double_loss_half_readme_sequence() -> None:
    assert lots_used(RiskMode.WIN_DOUBLE_LOSS_HALF, "W W L L")[:4] == d(
        "0.01", "0.02", "0.04", "0.02"
    )


def test_mode3_never_below_base() -> None:
    assert lots_used(RiskMode.WIN_DOUBLE_LOSS_HALF, "L L W L L") == d(
        "0.01", "0.01", "0.01", "0.02", "0.01", "0.01"
    )


def test_mode3_halving_keeps_fractional_theoretical() -> None:
    # 0.01 -> W 0.02 -> W 0.04 -> W 0.08 -> L 0.04 ... and 0.03 halves to 0.015, not rounded.
    state = ProgressionState(
        mode=RiskMode.WIN_DOUBLE_LOSS_HALF, base_lot=D("0.01"), theoretical_lot=D("0.03")
    )
    assert settle(state, L).theoretical_lot == D("0.015")


def test_mode4_always_double_readme_sequence() -> None:
    assert lots_used(RiskMode.ALWAYS_DOUBLE, "W L W L") == d("0.01", "0.02", "0.04", "0.08", "0.16")


def test_mode5_double_every_five_readme_sequence() -> None:
    used = lots_used(RiskMode.DOUBLE_EVERY_FIVE, "W L W W L L L W W L W W L L W")
    assert used[0:5] == [D("0.01")] * 5
    assert used[5:10] == [D("0.02")] * 5
    assert used[10:15] == [D("0.04")] * 5
    assert used[15] == D("0.08")


def test_mode5_outcome_does_not_matter() -> None:
    assert lots_used(RiskMode.DOUBLE_EVERY_FIVE, "W W W W W") == lots_used(
        RiskMode.DOUBLE_EVERY_FIVE, "L L L L L"
    )


def test_mode6_win_half_increment_readme_sequence() -> None:
    assert lots_used(RiskMode.WIN_HALF_INCREMENT_LOSS_HOLD, "W W L W") == d(
        "0.010000", "0.015000", "0.022500", "0.022500", "0.033750"
    )


# --- Settlement rules (§22, INV-02) -------------------------------------------------------


@pytest.mark.parametrize("mode", list(RiskMode))
@pytest.mark.parametrize("outcome", [BE, OT])
def test_breakeven_and_other_leave_every_mode_unchanged(
    mode: RiskMode, outcome: TradeOutcome
) -> None:
    # §22 V1 default. Includes Mode 4 and Mode 5's counter — see SPEC_NOTES Q-02.
    state = ProgressionState.initial(mode, D("0.01"))
    for o in (W, L, W):
        state = settle(state, o)
    assert settle(state, outcome) == state


def test_win_loss_counters() -> None:
    state = ProgressionState.initial(RiskMode.ANTI_MARTINGALE, D("0.01"))
    for o in (W, W, L, BE, OT):
        state = settle(state, o)
    assert (state.wins, state.losses, state.completed_trades) == (2, 1, 3)


def test_settle_is_pure() -> None:
    state = ProgressionState.initial(RiskMode.ANTI_MARTINGALE, D("0.01"))
    settle(state, W)
    assert state.theoretical_lot == D("0.01")


# --- Deriv-style bases and precision (§14, §16, INV-07) ------------------------------------


@pytest.mark.parametrize("base", ["0.0005", "0.001", "0.5", "0.01"])
@pytest.mark.parametrize("mode", list(RiskMode))
def test_modes_work_from_any_broker_base(base: str, mode: RiskMode) -> None:
    b = D(base)
    ref = [x / D("0.01") for x in lots_used(mode, "W W L W L L W", "0.01")]
    assert [x / b for x in lots_used(mode, "W W L W L L W", base)] == ref


def test_mode6_stays_exact_over_long_win_streak() -> None:
    state = ProgressionState.initial(RiskMode.WIN_HALF_INCREMENT_LOSS_HOLD, D("0.0005"))
    for _ in range(30):
        state = settle(state, W)
    assert Fraction(state.theoretical_lot) == Fraction("0.0005") * Fraction(3, 2) ** 30


def test_precision_overflow_raises_instead_of_rounding() -> None:
    state = ProgressionState.initial(RiskMode.WIN_HALF_INCREMENT_LOSS_HOLD, D("0.01"))
    for _ in range(42):  # 0.01 x 1.5^42 still fits in 50 significant digits
        state = settle(state, W)
    with pytest.raises(ArithmeticError):
        settle(state, W)


# --- State invariants ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_lot": D("0"), "theoretical_lot": D("0.01")},
        {"base_lot": D("0.01"), "theoretical_lot": D("0.005")},  # below base
        {"base_lot": 0.01, "theoretical_lot": D("0.01")},  # float
        {"base_lot": D("0.01"), "theoretical_lot": D("0.01"), "wins": -1},
        {"base_lot": D("0.01"), "theoretical_lot": D("0.01"), "mode5_block_trade_count": 5},
    ],
)
def test_invalid_state_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        ProgressionState(mode=RiskMode.DOUBLE_EVERY_FIVE, **kwargs)  # type: ignore[arg-type]


def test_mode_change_resets_to_base() -> None:
    # §33: switching method starts the new method from base state.
    s = ProgressionState.initial(RiskMode.MARTINGALE, D("0.5"))
    assert (s.theoretical_lot, s.wins, s.losses, s.mode5_block_trade_count) == (D("0.5"), 0, 0, 0)


@pytest.mark.parametrize(
    ("theoretical", "volume_min", "volume_max"),
    [("25", "0.01", "25"), ("55", "0.1", "50"), ("100", "0.5", "100")],
)
def test_progression_resets_at_dynamic_broker_maximum(
    theoretical: str, volume_min: str, volume_max: str
) -> None:
    """Catches using a hard-coded maximum or allowing an exact-max lot to execute."""
    state = ProgressionState(
        mode=RiskMode.DOUBLE_EVERY_FIVE,
        base_lot=D("0.01"),
        theoretical_lot=D(theoretical),
        wins=9,
        losses=2,
        mode5_block_trade_count=4,
    )

    reset, happened = progression.reset_at_broker_limit(
        state, volume_min=D(volume_min), volume_max=D(volume_max)
    )

    assert happened
    assert reset == ProgressionState.initial(RiskMode.DOUBLE_EVERY_FIVE, D(volume_min))


def test_progression_below_broker_maximum_is_unchanged() -> None:
    """Catches resetting one broker step early."""
    state = ProgressionState(
        mode=RiskMode.ALWAYS_DOUBLE, base_lot=D("0.01"), theoretical_lot=D("24.99"), wins=8
    )
    result, happened = progression.reset_at_broker_limit(
        state, volume_min=D("0.01"), volume_max=D("25")
    )
    assert not happened
    assert result is state
