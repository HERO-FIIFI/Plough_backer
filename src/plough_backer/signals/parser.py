"""Deterministic parser (README §9, §10). Regex only; anything uncertain is rejected.

Profiles are named per source (config/sources.yaml `parser_profile`). `standard_v1` covers
exactly the README §9 formats; each real channel gets its own profile module in
signals/profiles/ that rewrites its text into standard_v1 (see that package's docstring).

standard_v1 rules:
- exactly one line contains BUY or SELL (optionally + LIMIT/STOP) — the header;
- the header names exactly one configured symbol alias;
- header entry: one number, "NOW", or nothing (MARKET). Two numbers -> rejected (Q-14);
- a MARKET header price is the reference entry; only explicit LIMIT/STOP makes a pending order;
- "SL <n>" exactly once; "TP<k> <n>" lines; an unlabelled "TP <n>" is allowed only alone and
  never counts as TP1/TP2 (Q-04);
- other lines (commentary) are ignored.
"""

import importlib
import pkgutil
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from plough_backer.config import SymbolsConfig
from plough_backer.enums import Direction, OrderType, RejectionReason
from plough_backer.exceptions import InvalidSignal, ParseRejected
from plough_backer.signals import profiles
from plough_backer.signals.models import NormalizedSignal
from plough_backer.signals.symbol_resolver import SymbolResolver
from plough_backer.signals.validators import select_take_profit

_DIRECTION = re.compile(r"\b(BUY|SELL)\b(?:\s+(LIMIT|STOP)\b)?", re.IGNORECASE)
_SL = re.compile(r"^\s*SL\s*[:=]?\s*(\S+)\s*$", re.IGNORECASE)
_TP = re.compile(r"^\s*TP\s*(\d?)\s*[:=]?\s*(\S+)\s*$", re.IGNORECASE)
_NUMBERISH = re.compile(r"\d[\w.,]*")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    signal: NormalizedSignal
    labelled_take_profits: dict[int, Decimal]  # {1: TP1, 2: TP2, ...} for the journal


def _number(token: str, what: str) -> Decimal:
    if not _NUMBER.fullmatch(token):
        raise ParseRejected(RejectionReason.MALFORMED_NUMBER, f"{what}: {token!r}")
    try:
        return Decimal(token)
    except InvalidOperation:  # pragma: no cover — regex already guarantees validity
        raise ParseRejected(RejectionReason.MALFORMED_NUMBER, f"{what}: {token!r}") from None


def _order_type(direction: Direction, kind: str | None) -> OrderType:
    if kind is None:
        return OrderType.MARKET
    return OrderType(f"{direction}_{kind.upper()}")


def parse_standard_v1(
    raw: str,
    *,
    symbols: SymbolsConfig,
    signal_id: str,
    source_id: str,
    source_message_id: int,
    source_timestamp: datetime,
) -> ParsedMessage:
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    headers = [ln for ln in lines if _DIRECTION.search(ln)]
    if not headers:
        raise ParseRejected(RejectionReason.UNRECOGNIZED_FORMAT, "no BUY/SELL instruction")
    if len(headers) > 1 or len(_DIRECTION.findall(headers[0])) > 1:
        raise ParseRejected(RejectionReason.AMBIGUOUS_SIGNAL, "more than one BUY/SELL (Q-19)")
    header = headers[0]

    found = SymbolResolver(symbols).find(header)
    if not found:
        raise InvalidSignal(RejectionReason.UNSUPPORTED_SYMBOL, f"no known symbol: {header!r}")
    if len(found) > 1:
        raise InvalidSignal(RejectionReason.AMBIGUOUS_SIGNAL, "more than one symbol")
    symbol = found[0]

    match = _DIRECTION.search(header)
    assert match is not None
    direction = Direction(match.group(1).upper())
    order_type = _order_type(direction, match.group(2))

    rest = _DIRECTION.sub(" ", header.replace(symbol.alias, " ")).replace("@", " ")
    tokens = _NUMBERISH.findall(rest)
    if len(tokens) > 1:
        raise ParseRejected(RejectionReason.AMBIGUOUS_ENTRY, f"entry {tokens} (Q-14)")
    entry = _number(tokens[0], "entry") if tokens else None
    if order_type.is_pending and entry is None:
        raise InvalidSignal(RejectionReason.ENTRY_MISSING, f"{order_type} without a price")

    stop_losses: list[Decimal] = []
    labelled: dict[int, Decimal] = {}
    unlabelled: list[Decimal] = []
    for line in lines:
        if line is header:
            continue
        if sl := _SL.match(line):
            stop_losses.append(_number(sl.group(1), "SL"))
        elif tp := _TP.match(line):
            value = _number(tp.group(2), "TP")
            if not tp.group(1):
                unlabelled.append(value)
            elif int(tp.group(1)) in labelled:
                raise ParseRejected(RejectionReason.AMBIGUOUS_SIGNAL, f"TP{tp.group(1)} twice")
            else:
                labelled[int(tp.group(1))] = value

    if not stop_losses:
        raise InvalidSignal(RejectionReason.STOP_LOSS_MISSING, "Stop loss could not be determined.")
    if len(stop_losses) > 1:
        raise ParseRejected(RejectionReason.AMBIGUOUS_SIGNAL, "more than one SL")
    if unlabelled and (labelled or len(unlabelled) > 1):
        raise ParseRejected(RejectionReason.AMBIGUOUS_SIGNAL, "mixed or repeated unlabelled TP")

    selected = select_take_profit(labelled, symbol.mapping, symbols.gold)
    take_profits = tuple(labelled[k] for k in sorted(labelled)) or tuple(unlabelled)
    signal = NormalizedSignal(
        signal_id=signal_id,
        source_id=source_id,
        source_message_id=source_message_id,
        source_timestamp=source_timestamp,
        symbol_raw=symbol.alias,
        symbol_mt5=symbol.mapping.mt5_symbol,
        direction=direction,
        order_type=order_type,
        entry=entry,
        stop_loss=stop_losses[0],
        take_profits=take_profits,
        selected_take_profit=selected,
        parser_version=PARSER_VERSIONS["standard_v1"],
        raw_message=raw,
    )
    return ParsedMessage(signal, labelled)


Parser = Callable[..., ParsedMessage]
PARSER_VERSIONS = {"standard_v1": "standard_v1.0"}


def _rewrite_profile(rewrites: list[tuple[str, str]], version: str) -> Parser:
    rules = [(re.compile(p, re.MULTILINE | re.IGNORECASE), r) for p, r in rewrites]

    def parse(raw: str, **kwargs: Any) -> ParsedMessage:
        text = raw
        for pattern, replacement in rules:
            text = pattern.sub(replacement, text)
        parsed = parse_standard_v1(text, **kwargs)
        # journal the provider's original text, stamped with the profile's version
        signal = replace(parsed.signal, raw_message=raw, parser_version=version)
        return ParsedMessage(signal, parsed.labelled_take_profits)

    return parse


def _load_profiles() -> dict[str, Parser]:
    found: dict[str, Parser] = {"standard_v1": parse_standard_v1}
    for info in pkgutil.iter_modules(profiles.__path__):
        module = importlib.import_module(f"{profiles.__name__}.{info.name}")
        found[info.name] = _rewrite_profile(module.REWRITES, module.VERSION)
    return found


PROFILES = _load_profiles()


def get_parser(profile: str) -> Parser:
    try:
        return PROFILES[profile]
    except KeyError:
        reason = RejectionReason.UNRECOGNIZED_FORMAT
        raise ParseRejected(reason, f"unknown profile {profile!r}") from None
