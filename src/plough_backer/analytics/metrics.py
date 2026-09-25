"""Performance metrics (README §44–§46). Pure functions over settled trade records."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from plough_backer.enums import TradeOutcome

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeRecord:
    closed_at: datetime
    source_id: str
    symbol: str
    result: TradeOutcome
    net_profit: Decimal
    r_multiple: Decimal | None
    executed_lot: Decimal
    estimated_risk: Decimal
    equity_risk_percent: Decimal | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Metrics:
    starting_balance: Decimal
    ending_balance: Decimal
    net_pl: Decimal
    return_pct: Decimal | None
    total_trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate: Decimal | None  # wins / (wins + losses)
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    average_r: Decimal | None
    expectancy_r: Decimal | None
    total_r: Decimal
    largest_win: Decimal | None
    largest_loss: Decimal | None
    longest_win_streak: int
    longest_loss_streak: int
    peak_equity: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    average_lot: Decimal | None
    max_lot: Decimal | None
    average_risk: Decimal | None
    max_risk: Decimal | None
    average_exposure_pct: Decimal | None
    max_exposure_pct: Decimal | None


def _avg(values: list[Decimal]) -> Decimal | None:
    return sum(values, ZERO) / len(values) if values else None


def _streak(results: list[TradeOutcome], target: TradeOutcome) -> int:
    best = run = 0
    for r in results:
        if r is target:
            run += 1
            best = max(best, run)
        elif r in (TradeOutcome.WIN, TradeOutcome.LOSS):
            run = 0  # BREAKEVEN/OTHER neither extends nor breaks a streak
    return best


def compute(trades: Iterable[TradeRecord], starting_balance: Decimal) -> Metrics:
    ts = sorted(trades, key=lambda t: t.closed_at)
    results = [t.result for t in ts]
    wins = [t.net_profit for t in ts if t.result is TradeOutcome.WIN]
    losses = [t.net_profit for t in ts if t.result is TradeOutcome.LOSS]
    gross_profit = sum((p for p in (t.net_profit for t in ts) if p > 0), ZERO)
    gross_loss = sum((p for p in (t.net_profit for t in ts) if p < 0), ZERO)
    net = sum((t.net_profit for t in ts), ZERO)
    rs = [t.r_multiple for t in ts if t.r_multiple is not None]

    balance = peak = starting_balance
    max_dd = max_dd_pct = ZERO
    for t in ts:
        balance += t.net_profit
        peak = max(peak, balance)
        max_dd = max(max_dd, peak - balance)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - balance) / peak * 100)

    decided = len(wins) + len(losses)
    lots = [t.executed_lot for t in ts]
    risks = [t.estimated_risk for t in ts]
    exposures = [t.equity_risk_percent for t in ts if t.equity_risk_percent is not None]
    return Metrics(
        starting_balance=starting_balance,
        ending_balance=starting_balance + net,
        net_pl=net,
        return_pct=net / starting_balance * 100 if starting_balance else None,
        total_trades=len(ts),
        wins=len(wins),
        losses=len(losses),
        breakeven=results.count(TradeOutcome.BREAKEVEN),
        win_rate=Decimal(len(wins)) / decided * 100 if decided else None,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=gross_profit / -gross_loss if gross_loss else None,
        average_win=_avg(wins),
        average_loss=_avg(losses),
        average_r=_avg(rs),
        expectancy_r=_avg(rs),  # mean realized R per trade = expectancy in R
        total_r=sum(rs, ZERO),
        largest_win=max(wins, default=None),
        largest_loss=min(losses, default=None),
        longest_win_streak=_streak(results, TradeOutcome.WIN),
        longest_loss_streak=_streak(results, TradeOutcome.LOSS),
        peak_equity=peak,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        average_lot=_avg(lots),
        max_lot=max(lots, default=None),
        average_risk=_avg(risks),
        max_risk=max(risks, default=None),
        average_exposure_pct=_avg(exposures),
        max_exposure_pct=max(exposures, default=None),
    )


def breakdown(
    trades: Iterable[TradeRecord], key: Callable[[TradeRecord], str]
) -> dict[str, Metrics]:
    """§45/§46 per-source / per-instrument metrics (P/L-only, starting balance 0)."""
    groups: dict[str, list[TradeRecord]] = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    return {k: compute(v, ZERO) for k, v in sorted(groups.items())}
