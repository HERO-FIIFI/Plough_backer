"""Alias -> MT5 symbol (README §11). Broker naming lives in config/symbols.yaml, never code."""

import re
from dataclasses import dataclass

from plough_backer.config import SymbolMapping, SymbolsConfig


@dataclass(frozen=True, slots=True)
class ResolvedSymbol:
    name: str  # config key, e.g. "GOLD"
    alias: str  # text matched in the message
    mapping: SymbolMapping


class SymbolResolver:
    def __init__(self, config: SymbolsConfig) -> None:
        pairs = [(alias, name, m) for name, m in config.symbols.items() for alias in m.aliases]
        # Longest alias first so "BOOM 1000" wins over a shorter "BOOM".
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        self._patterns = [
            (re.compile(rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])", re.IGNORECASE), a, n, m)
            for a, n, m in pairs
        ]

    def find(self, text: str) -> list[ResolvedSymbol]:
        """All distinct configured symbols mentioned in `text` (whole-word, case-insensitive)."""
        found: dict[str, ResolvedSymbol] = {}
        remaining = text
        for pattern, _alias, name, mapping in self._patterns:
            match = pattern.search(remaining)
            if match and name not in found:
                found[name] = ResolvedSymbol(name, match.group(0), mapping)
                remaining = pattern.sub(" ", remaining)
        return list(found.values())
