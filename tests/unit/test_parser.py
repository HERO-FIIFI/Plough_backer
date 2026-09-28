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


# --- Provider profiles (signals/profiles/) ------------------------------------------------

LOVEROCK_BUY = """XAUUSD BUY NOW ( 4300 ) ✔️

📊TARGET 1  ( 4306 )✔️
📊TARGET 2  ( 4312 )✔️
📊TARGET 3  ( 4320 )✔️

🚫 STOP LOSS   ( 4290 )

RISK MANAGEMENT IS IMPORTANT✔️"""

LOVEROCK_SELL = """📊XAUUSD SELL NOW
( 4202 ) ✅
📊TARGET 1  ( 4198 )✅
📊TARGET 2  ( 4194 )✅
📊TARGET 3  ( 4190 )✅
📊TARGET 4  ( 4180 )✅

🚫 STOP LOSS   (  4214  )

RISK MANAGEMENT IS IMPORTANT ✅"""


def parse_loverock(message: str) -> Any:
    return get_parser("loverock_fx")(
        message,
        symbols=SYMBOLS,
        signal_id="PB-000001",
        source_id="loverock",
        source_message_id=1,
        source_timestamp=TS,
    )


@pytest.mark.parametrize(
    ("message", "direction", "entry", "sl", "tps"),
    [
        (LOVEROCK_BUY, "BUY", "4300", "4290", ["4306", "4312", "4320"]),
        (LOVEROCK_SELL, "SELL", "4202", "4214", ["4198", "4194", "4190", "4180"]),
    ],
)
def test_loverock_profile(
    message: str, direction: str, entry: str, sl: str, tps: list[str]
) -> None:
    s = parse_loverock(message).signal
    assert s.direction == direction
    assert s.order_type == "MARKET"
    assert s.entry == Decimal(entry)
    assert s.stop_loss == Decimal(sl)
    assert list(s.take_profits) == [Decimal(t) for t in tps]
    assert s.raw_message == message  # journal keeps the provider's original text
    assert s.parser_version == "loverock_fx.1"


@pytest.mark.parametrize("message", ["Tp1 hit", "Sl hit", "All tp hit"])
def test_loverock_follow_ups_are_not_signals(message: str) -> None:
    with pytest.raises(ParseRejected):
        parse_loverock(message)


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
