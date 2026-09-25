"""Fixture-driven parser tests (README §62) + fingerprint (§24)."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from plough_backer.config import SymbolsConfig
from plough_backer.exceptions import InvalidSignal, ParseRejected
from plough_backer.signals.fingerprint import fingerprint
from plough_backer.signals.parser import get_parser

FIXTURES = yaml.safe_load(
    (Path(__file__).parents[1] / "fixtures" / "parser_cases.yaml").read_text(encoding="utf-8")
)
SYMBOLS = SymbolsConfig.model_validate(FIXTURES["symbols"])
TS = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
KIND = {"parse": ParseRejected, "invalid": InvalidSignal}


def parse(message: str, message_id: int = 1) -> Any:
    return get_parser("standard_v1")(
        message,
        symbols=SYMBOLS,
        signal_id="PB-000001",
        source_id="gold_signals",
        source_message_id=message_id,
        source_timestamp=TS,
    )


def opt(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda c: c["name"])
def test_fixture(case: dict[str, Any]) -> None:
    expect = case["expect"]
    if "rejected" in expect:
        with pytest.raises(KIND[expect["rejected"]["kind"]]) as exc:
            parse(case["message"])
        # exact class, not a subclass sibling: parse vs invalid decides the terminal state
        assert type(exc.value) is KIND[expect["rejected"]["kind"]]
        assert exc.value.reason == expect["rejected"]["reason"]
        return
    s = parse(case["message"]).signal
    assert s.symbol_mt5 == expect["symbol"]
    assert s.direction == expect["direction"]
    assert s.order_type == expect["order_type"]
    assert s.entry == opt(expect["entry"])
    assert s.stop_loss == Decimal(expect["sl"])
    assert list(s.take_profits) == [Decimal(t) for t in expect["tps"]]
    assert s.selected_take_profit == Decimal(expect["selected"])
    assert s.raw_message == case["message"]
    assert s.parser_version == "standard_v1.0"


def test_gold_keeps_all_tps_for_journal() -> None:
    parsed = parse("BUY GOLD 4500\nSL 4492\nTP1 4506\nTP2 4512\nTP3 4518")
    assert parsed.labelled_take_profits == {1: 4506, 2: 4512, 3: 4518}


def test_parsing_is_deterministic() -> None:
    msg = "XAUUSD BUY @ 4500\nSL: 4492\nTP1: 4506\nTP2: 4512"
    assert parse(msg).signal == parse(msg).signal


def test_unknown_profile_fails_closed() -> None:
    with pytest.raises(ParseRejected):
        get_parser("nope")


# --- Fingerprint --------------------------------------------------------------------------


def test_fingerprint_is_stable_sha256() -> None:
    s = parse("BUY GOLD 4500\nSL 4492\nTP1 4506\nTP2 4512").signal
    fp = fingerprint(s)
    assert len(fp) == 64
    assert fp == fingerprint(parse("BUY GOLD 4500\nSL 4492\nTP1 4506\nTP2 4512").signal)


def test_fingerprint_ignores_price_formatting() -> None:
    a = parse("BUY GOLD 4500\nSL 4492\nTP1 4506\nTP2 4512").signal
    b = parse("BUY GOLD 4500.00\nSL 4492.0\nTP1 4506\nTP2 4512.0").signal
    assert fingerprint(a) == fingerprint(b)


@pytest.mark.parametrize(
    "change",
    [
        {"source_message_id": 2},
        {"source_id": "other"},
        {"stop_loss": Decimal("4491")},
        {"entry": Decimal("4501")},
    ],
)
def test_fingerprint_changes_with_candidate_fields(change: dict[str, Any]) -> None:
    s = parse("BUY GOLD 4500\nSL 4492\nTP1 4506\nTP2 4512").signal
    assert fingerprint(s) != fingerprint(replace(s, **change))


def test_edited_message_changes_fingerprint() -> None:
    # §25/§62 "edited message": the edit is a different instruction; Phase 6 must not
    # execute it automatically once the original executed (Q-13 for pre-execution edits).
    original = parse("BUY GOLD 4500\nSL 4492\nTP1 4506\nTP2 4512").signal
    edited = parse("BUY GOLD 4500\nSL 4490\nTP1 4506\nTP2 4512").signal
    assert fingerprint(original) != fingerprint(edited)
