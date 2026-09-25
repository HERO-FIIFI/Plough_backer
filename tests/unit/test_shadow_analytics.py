"""Phase 9 Shadow Mode / replay (§36–§38, §67) and Phase 10 metrics (§44). Hand-computed."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from plough_backer.analytics.metrics import TradeRecord, breakdown, compute
from plough_backer.enums import RiskMode, ShadowStatus, TradeOutcome, VolumeMaxPolicy
from plough_backer.shadow.engine import replay
from tests.fakes import GOLD

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "historical_signals.json"
D = Decimal


def load_script():  # type: ignore[no-untyped-def]
    import importlib.util

    spec = importlib.util.spec_from_file_location("replay_script", ROOT / "scripts" / "replay.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replay_matches_hand_computed_results() -> None:
    # $1 per 0.01 move per lot; 8-point SL, 12-point TP; margin = price*100*lot/1000.
    p = load_script().run(FIXTURE)
    m1, m2, m4 = p[RiskMode.ANTI_MARTINGALE], p[RiskMode.MARTINGALE], p[RiskMode.ALWAYS_DOUBLE]
    # Mode 1: lots .01 W .02 W .04 L .01 W .02 L (BE skipped) .01 W
    assert (m1.balance, m1.wins, m1.losses, m1.max_lot) == (D(112), 4, 2, D("0.04"))
    assert (m1.peak_equity, m1.max_drawdown) == (D(136), D(36))
    # Mode 2: .01 W .01 W .01 L .02 W .01 L .02 W
    assert m2.balance == D(156)
    # Mode 4: .01 .02 .04 .08 .16 -> balance 72, then .32 needs $144 margin -> fails at trade 6
    assert m4.status is ShadowStatus.SHADOW_FAILED
    assert (m4.failed_at_trade, m4.failure_reason) == (6, "Insufficient simulated margin")
    assert m4.balance == D(72)
    assert m4.trades == 5  # never pretends it continued (§38)


def test_replay_is_deterministic_and_modes_independent() -> None:
    a, b = load_script().run(FIXTURE), load_script().run(FIXTURE)
    assert {m: (p.balance, p.states) for m, p in a.items()} == {
        m: (p.balance, p.states) for m, p in b.items()
    }
    assert len({p.balance for p in a.values()}) > 1


def test_replay_cli_prints_all_six_methods() -> None:
    out = subprocess.run(  # noqa: S603 — fixed local script
        [sys.executable, str(ROOT / "scripts" / "replay.py"), "--input", str(FIXTURE)],
        capture_output=True,
        text=True,
        check=True,
    )
    for mode in RiskMode:
        assert mode.label in out.stdout
    assert "trade 6: Insufficient simulated margin" in out.stdout


def test_empty_replay_keeps_starting_balance() -> None:
    p = replay(
        [],
        starting_balance=D(100),
        profit=lambda *a: D(0),
        margin=lambda *a: D(0),
        max_policy=VolumeMaxPolicy.CAP,
    )
    assert all(x.balance == D(100) and x.trades == 0 for x in p.values())
    assert GOLD.volume_min == D("0.01")


# --- metrics ------------------------------------------------------------------------------

T0 = datetime(2026, 9, 14, tzinfo=UTC)


def rec(
    i: int,
    result: TradeOutcome,
    net: str,
    r: str | None,
    symbol: str = "XAUUSD",
    source: str = "gold",
) -> TradeRecord:
    return TradeRecord(
        closed_at=T0 + timedelta(hours=i),
        source_id=source,
        symbol=symbol,
        result=result,
        net_profit=D(net),
        r_multiple=None if r is None else D(r),
        executed_lot=D("0.01") * i,
        estimated_risk=D(8) * i,
        equity_risk_percent=D(i),
    )


W, L, BE = TradeOutcome.WIN, TradeOutcome.LOSS, TradeOutcome.BREAKEVEN
TRADES = [
    rec(1, W, "12", "1.5"),
    rec(2, W, "24", "1.5"),
    rec(3, L, "-32", "-1"),
    rec(4, BE, "0", "0"),
    rec(5, W, "60", "1.5", symbol="Boom 1000 Index", source="deriv"),
    rec(6, L, "-48", "-1", symbol="Boom 1000 Index", source="deriv"),
]


def test_metrics_core_numbers() -> None:
    m = compute(TRADES, D(100))
    assert (m.net_pl, m.ending_balance, m.return_pct) == (D(16), D(116), D(16))
    assert (m.total_trades, m.wins, m.losses, m.breakeven) == (6, 3, 2, 1)
    assert m.win_rate == D(60)  # 3 / 5 decided
    assert (m.gross_profit, m.gross_loss) == (D(96), D(-80))
    assert m.profit_factor == D("1.2")
    assert (m.largest_win, m.largest_loss) == (D(60), D(-48))
    assert m.total_r == D("2.5")
    assert m.average_r == D("2.5") / 6
    assert (m.longest_win_streak, m.longest_loss_streak) == (2, 1)
    # equity: 112, 136, 104, 104, 164, 116 -> peak 164, max DD 48
    assert (m.peak_equity, m.max_drawdown) == (D(164), D(48))
    assert (m.max_lot, m.max_risk, m.max_exposure_pct) == (D("0.06"), D(48), D(6))


def test_breakeven_does_not_break_a_streak() -> None:
    m = compute([rec(1, W, "1", None), rec(2, BE, "0", None), rec(3, W, "1", None)], D(10))
    assert m.longest_win_streak == 2


def test_empty_metrics_do_not_divide_by_zero() -> None:
    m = compute([], D(0))
    assert (m.win_rate, m.profit_factor, m.return_pct, m.average_r) == (None, None, None, None)


def test_source_and_instrument_breakdowns() -> None:
    by_source = breakdown(TRADES, lambda t: t.source_id)
    assert (by_source["gold"].net_pl, by_source["deriv"].net_pl) == (D(4), D(12))
    by_symbol = breakdown(TRADES, lambda t: t.symbol)
    assert by_symbol["Boom 1000 Index"].wins == 1
    assert by_symbol["XAUUSD"].win_rate == D(2) / 3 * 100
