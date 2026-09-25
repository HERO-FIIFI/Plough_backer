from decimal import Decimal

import pytest

from plough_backer.decimal_utils import DecimalConversionError, decimal_to_str, to_decimal


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.01, "0.01"),  # MT5 volume_step arrives as float
        (0.1, "0.1"),
        (0.0005, "0.0005"),  # Deriv synthetic volume_min values, as MT5 floats
        (0.001, "0.001"),
        (0.5, "0.5"),
        (5e-05, "0.00005"),  # float repr is exponent form; value must stay exact
        (4500.2, "4500.2"),
        ("0.050625", "0.050625"),
        (" 4492 ", "4492"),
        (3, "3"),
        (Decimal("0.022500"), "0.022500"),
    ],
)
def test_to_decimal_is_exact(raw: object, expected: str) -> None:
    result = to_decimal(raw)  # type: ignore[arg-type]
    assert result == Decimal(expected)
    assert str(result) == expected


@pytest.mark.parametrize("bad", [True, "abc", "", "NaN", float("inf"), float("nan"), "-Infinity"])
def test_to_decimal_rejects_non_finite_and_non_numbers(bad: object) -> None:
    with pytest.raises(DecimalConversionError):
        to_decimal(bad)  # type: ignore[arg-type]


def test_decimal_to_str_never_uses_exponent_and_keeps_precision() -> None:
    assert decimal_to_str(Decimal("1E-7")) == "0.0000001"
    assert decimal_to_str(Decimal("0.010000")) == "0.010000"
    assert decimal_to_str(Decimal("1.5E+3")) == "1500"
    with pytest.raises(DecimalConversionError):
        decimal_to_str(Decimal("NaN"))
