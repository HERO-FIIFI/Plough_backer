"""Phase 2: normalization (§16, §17, §61), risk (§18, §19), Equity Lock (§20, §63)."""

from decimal import Decimal

import pytest

from plough_backer.enums import Direction, RiskMode, TradeOutcome, VolumeMaxPolicy
from plough_backer.exceptions import BrokerError, InvalidSymbolSpecification, VolumeAboveMaximum
from plough_backer.risk.engine import assess_risk
from plough_backer.risk.equity_lock import check_equity_lock
from plough_backer.risk.normalization import normalize_volume
from plough_backer.risk.progression import ProgressionState, settle
from tests.fakes import BOOM, GOLD, V75, FakeGateway

D = Decimal
CAP, REJECT = VolumeMaxPolicy.CAP, VolumeMaxPolicy.REJECT


def norm(theoretical: str, vmin: str = "0.01", vmax: str = "100", step: str = "0.01", policy=CAP):  # type: ignore[no-untyped-def]
    return normalize_volume(D(theoretical), D(vmin), D(vmax), D(step), max_policy=policy)


# --- Normalization ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("theoretical", "executable"),
    [  # README §16 / §61 table, verbatim
        ("0.010000", "0.01"),
        ("0.015000", "0.02"),
        ("0.022500", "0.02"),
        ("0.033750", "0.03"),
        ("0.050625", "0.05"),
    ],
)
def test_readme_normalization_table(theoretical: str, executable: str) -> None:
    decision = norm(theoretical)
    assert decision.executable == D(executable)
    assert str(decision.theoretical) == theoretical  # INV-06: theoretical untouched
    assert not decision.capped_at_max


def test_mode6_progression_survives_normalization() -> None:
    # INV-06 end to end: rounding each trade never feeds back into the progression.
    state = ProgressionState.initial(RiskMode.WIN_HALF_INCREMENT_LOSS_HOLD, D("0.01"))
    pairs = []
    for outcome in (TradeOutcome.WIN, TradeOutcome.WIN, TradeOutcome.WIN, TradeOutcome.WIN):
        pairs.append((state.theoretical_lot, norm(str(state.theoretical_lot)).executable))
        state = settle(state, outcome)
    assert pairs == [
        (D("0.01"), D("0.01")),
        (D("0.015"), D("0.02")),
        (D("0.0225"), D("0.02")),
        (D("0.03375"), D("0.03")),
    ]
    assert state.theoretical_lot == D("0.050625")


@pytest.mark.parametrize(
    ("theoretical", "vmin", "step", "expected"),
    [
        ("0.0005", "0.0005", "0.0001", "0.0005"),  # Deriv-style tiny minimum
        ("0.00075", "0.0005", "0.0001", "0.0008"),
        ("0.0015", "0.001", "0.001", "0.002"),
        ("0.75", "0.5", "0.01", "0.75"),
        ("0.001", "0.5", "0.01", "0.5"),  # never below volume_min
        ("0.004", "0.01", "0.01", "0.01"),  # rounds to 0 steps -> floored to min
    ],
)
def test_deriv_style_specs(theoretical: str, vmin: str, step: str, expected: str) -> None:
    assert norm(theoretical, vmin=vmin, step=step).executable == D(expected)


def test_long_precision_theoretical_rounds_correctly() -> None:
    state = ProgressionState.initial(RiskMode.WIN_HALF_INCREMENT_LOSS_HOLD, D("0.01"))
    for _ in range(30):
        state = settle(state, TradeOutcome.WIN)
    # 0.01 x 1.5^30 = 1917.49...; a 28-digit context would still agree, 100 digits guarantees it
    assert norm(str(state.theoretical_lot), vmax="100000").executable == D("1917.51")


def test_above_max_cap_is_explicit() -> None:
    decision = norm("150", vmax="100")
    assert decision.executable == D("100")
    assert decision.capped_at_max
    assert decision.theoretical == D("150")


def test_cap_lands_on_step_grid_when_max_is_not() -> None:
    assert norm("5", vmax="1.005", step="0.01").executable == D("1.00")


def test_above_max_reject_is_a_broker_error() -> None:
    with pytest.raises(VolumeAboveMaximum) as exc:
        norm("150", vmax="100", policy=REJECT)
    assert isinstance(exc.value, BrokerError)  # §21: not a loss, never advances progression


@pytest.mark.parametrize(
    ("vmin", "vmax", "step"),
    [("0.015", "100", "0.01"), ("0", "100", "0.01"), ("0.01", "100", "0"), ("1", "0.5", "0.01")],
)
def test_invalid_specs_fail_closed(vmin: str, vmax: str, step: str) -> None:
    with pytest.raises(InvalidSymbolSpecification):
        norm("0.02", vmin=vmin, vmax=vmax, step=step)


def test_float_inputs_rejected() -> None:
    with pytest.raises(TypeError):
        normalize_volume(0.02, D("0.01"), D("100"), D("0.01"), max_policy=CAP)  # type: ignore[arg-type]


# --- Risk ---------------------------------------------------------------------------------


def test_readme_trade_notification_numbers() -> None:
    # §40: 0.04 lot, entry 4500.20, SL 4492.20, TP2 4512.20 -> $32.00 / $48.00 / 1:1.50
    r = assess_risk(
        FakeGateway(),
        symbol=GOLD.name,
        direction=Direction.BUY,
        executed_lot=D("0.04"),
        entry_price=D("4500.20"),
        stop_loss=D("4492.20"),
        take_profit=D("4512.20"),
        equity=D("480"),
    )
    assert r.risk_to_sl == D("32.00")
    assert r.reward_to_tp == D("48.00")
    assert r.reward_risk_ratio == D("1.5")
    assert r.equity_risk_percent is not None
    assert round(r.equity_risk_percent, 1) == D("6.7")  # §40 "Equity Risk: 6.7%"


def test_sell_risk_is_positive_money() -> None:
    r = assess_risk(
        FakeGateway(),
        symbol=GOLD.name,
        direction=Direction.SELL,
        executed_lot=D("0.01"),
        entry_price=D("4500"),
        stop_loss=D("4508"),
        take_profit=D("4488"),
        equity=D("800"),
    )
    assert (r.risk_to_sl, r.reward_to_tp, r.reward_risk_ratio) == (D("8"), D("12"), D("1.5"))


def test_same_lot_different_symbol_different_risk() -> None:
    # INV-09: risk comes from the symbol spec, never from lot size alone.
    kwargs = dict(
        direction=Direction.BUY,
        executed_lot=D("0.5"),
        entry_price=D("1000"),
        stop_loss=D("990"),
        take_profit=D("1020"),
        equity=D("800"),
    )
    boom = assess_risk(FakeGateway(), symbol=BOOM.name, **kwargs)  # type: ignore[arg-type]
    v75 = assess_risk(FakeGateway(), symbol=V75.name, **kwargs)  # type: ignore[arg-type]
    assert boom.risk_to_sl != v75.risk_to_sl


def test_zero_risk_and_zero_equity_do_not_crash() -> None:
    r = assess_risk(
        FakeGateway(),
        symbol=GOLD.name,
        direction=Direction.BUY,
        executed_lot=D("0.01"),
        entry_price=D("4500"),
        stop_loss=D("4500"),
        take_profit=D("4510"),
        equity=D("0"),
    )
    assert r.reward_risk_ratio is None
    assert r.equity_risk_percent is None


# --- Equity Lock (§20, §63) ---------------------------------------------------------------


def test_readme_equity_lock_allows() -> None:
    d = check_equity_lock(
        enabled=True, protected_equity=D(500), current_equity=D(800), risk_to_sl=D(200)
    )
    assert d.allowed
    assert d.projected_equity == D(600)


def test_readme_equity_lock_blocks() -> None:
    d = check_equity_lock(
        enabled=True, protected_equity=D(500), current_equity=D(800), risk_to_sl=D(350)
    )
    assert not d.allowed
    assert d.projected_equity == D(450)


def test_projected_equal_to_lock_is_allowed_provisionally() -> None:
    # §20 literal rule is "projected < protected blocks"; equality allowed — Q-07.
    assert check_equity_lock(
        enabled=True, protected_equity=D(500), current_equity=D(800), risk_to_sl=D(300)
    ).allowed


def test_disabled_lock_never_blocks_even_huge_risk() -> None:
    # INV-10: high risk alone never blocks.
    assert check_equity_lock(
        enabled=False, protected_equity=None, current_equity=D(800), risk_to_sl=D(5000)
    ).allowed


def test_enabled_lock_without_value_fails_closed() -> None:
    with pytest.raises(ValueError, match="protected equity"):
        check_equity_lock(enabled=True, protected_equity=None, current_equity=D(1), risk_to_sl=D(1))
