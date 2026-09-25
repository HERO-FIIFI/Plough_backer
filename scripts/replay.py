"""Replay runner (README §67): all six methods over one chronological signal file.

Usage: python scripts/replay.py --input tests/fixtures/historical_signals.json
Input JSON: {"starting_balance": "100", "volume_max_policy": "CAP"|"REJECT",
             "leverage": "100",   # margin = price * contract_size * lot / leverage (Q-08)
             "symbols": {name: {volume_min, volume_max, volume_step, tick_size, tick_value,
                                contract_size}},
             "signals": [{symbol, direction, entry, sl, tp, outcome}, ...]}  # chronological
P/L is tick-based: (exit - entry) / tick_size * tick_value * lot (sign by direction).
"""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from plough_backer.enums import Direction, TradeOutcome, VolumeMaxPolicy
from plough_backer.shadow.engine import ShadowPortfolio, ShadowSignal, replay
from plough_backer.trading.gateway import SymbolSpecification

D = Decimal


def load(path: Path) -> tuple[list[ShadowSignal], dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    specs = {
        name: SymbolSpecification(
            name=name,
            volume_min=D(str(s["volume_min"])),
            volume_max=D(str(s["volume_max"])),
            volume_step=D(str(s["volume_step"])),
            trade_tick_size=D(str(s["tick_size"])),
            trade_tick_value=D(str(s["tick_value"])),
            trade_contract_size=D(str(s["contract_size"])),
            digits=2,
            point=D(str(s["tick_size"])),
            trade_stops_level=0,
            trade_mode=4,
        )
        for name, s in data["symbols"].items()
    }
    signals = [
        ShadowSignal(
            symbol=x["symbol"],
            direction=Direction(x["direction"]),
            entry=D(str(x["entry"])),
            stop_loss=D(str(x["sl"])),
            take_profit=D(str(x["tp"])),
            outcome=TradeOutcome(x["outcome"]),
            spec=specs[x["symbol"]],
        )
        for x in data["signals"]
    ]
    return signals, data


def run(path: Path) -> dict[Any, ShadowPortfolio]:
    signals, data = load(path)
    specs = {s.symbol: s.spec for s in signals}
    leverage = D(str(data["leverage"]))

    def profit(sym: str, d: Direction, vol: D, o: D, c: D) -> D:
        spec = specs[sym]
        move = c - o if d is Direction.BUY else o - c
        return move / spec.trade_tick_size * spec.trade_tick_value * vol

    def margin(sym: str, d: Direction, vol: D, price: D) -> D:
        return price * specs[sym].trade_contract_size * vol / leverage

    return replay(
        signals,
        starting_balance=D(str(data["starting_balance"])),
        profit=profit,
        margin=margin,
        max_policy=VolumeMaxPolicy(data["volume_max_policy"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    print(
        f"{'mode':<24}{'balance':>12}{'W':>4}{'L':>4}{'max lot':>10}"
        f"{'max exp%':>10}{'max DD':>10}  failure"
    )
    for mode, p in run(args.input).items():
        failure = f"trade {p.failed_at_trade}: {p.failure_reason}" if p.failed_at_trade else "—"
        print(
            f"{mode.label:<24}{p.balance:>12.2f}{p.wins:>4}{p.losses:>4}{p.max_lot:>10}"
            f"{p.max_exposure_pct:>10.1f}{p.max_drawdown:>10.2f}  {failure}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
