"""Shadow Mode (README §36–§38, §67): all six methods replay the same chronological signals.

Pure and deterministic. Each portfolio keeps its own balance, progression and stats. Live
shadow state is recomputed from the journal in order rather than stored (the journal is
the source of truth), so it can never drift or be double-applied.

Open inputs (Q-08) are explicit: starting balance and the margin model are parameters.
A trade's outcome is the real outcome of that signal; P/L is priced at SL (loss) or the
selected TP (win) for each portfolio's own lot. BREAKEVEN/OTHER trades are skipped for P/L
and progression (§22) — they carry no deterministic exit price.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from plough_backer.enums import Direction, RiskMode, ShadowStatus, TradeOutcome, VolumeMaxPolicy
from plough_backer.exceptions import BrokerError
from plough_backer.risk.normalization import normalize_volume
from plough_backer.risk.progression import ProgressionState, settle
from plough_backer.trading.gateway import SymbolSpecification

# (symbol, direction, volume, price_open, price_close) -> P/L, e.g. BrokerGateway.calc_profit
ProfitFn = Callable[[str, Direction, Decimal, Decimal, Decimal], Decimal]
# (symbol, direction, volume, price) -> margin required
MarginFn = Callable[[str, Direction, Decimal, Decimal], Decimal]


@dataclass(frozen=True, slots=True, kw_only=True)
class ShadowSignal:
    symbol: str
    direction: Direction
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    outcome: TradeOutcome
    spec: SymbolSpecification


@dataclass(slots=True)
class ShadowPortfolio:
    mode: RiskMode
    starting_balance: Decimal
    balance: Decimal
    states: dict[str, ProgressionState] = field(default_factory=dict)  # per symbol (Q-01 a)
    wins: int = 0
    losses: int = 0
    trades: int = 0
    peak_equity: Decimal = Decimal(0)
    max_drawdown: Decimal = Decimal(0)
    max_drawdown_pct: Decimal = Decimal(0)
    max_lot: Decimal = Decimal(0)
    max_exposure_pct: Decimal = Decimal(0)
    status: ShadowStatus = ShadowStatus.ACTIVE
    failed_at_trade: int | None = None
    failure_reason: str | None = None

    @property
    def return_pct(self) -> Decimal:
        return (self.balance - self.starting_balance) / self.starting_balance * 100


def new_portfolios(starting_balance: Decimal) -> dict[RiskMode, ShadowPortfolio]:
    return {
        m: ShadowPortfolio(m, starting_balance, starting_balance, peak_equity=starting_balance)
        for m in RiskMode
    }


def apply_signal(
    p: ShadowPortfolio,
    sig: ShadowSignal,
    *,
    profit: ProfitFn,
    margin: MarginFn,
    max_policy: VolumeMaxPolicy,
) -> None:
    if p.status is ShadowStatus.SHADOW_FAILED:
        return  # §38: a failed portfolio does not pretend to continue
    if sig.outcome not in (TradeOutcome.WIN, TradeOutcome.LOSS):
        return
    state = p.states.get(sig.symbol) or ProgressionState.initial(p.mode, sig.spec.volume_min)
    n = p.trades + 1
    try:
        lot = normalize_volume(
            state.theoretical_lot,
            sig.spec.volume_min,
            sig.spec.volume_max,
            sig.spec.volume_step,
            max_policy=max_policy,
        ).executable
    except BrokerError as exc:
        _fail(p, n, f"Broker volume limit: {exc}")
        return
    if margin(sig.symbol, sig.direction, lot, sig.entry) > p.balance:
        _fail(p, n, "Insufficient simulated margin")
        return

    risk = abs(profit(sig.symbol, sig.direction, lot, sig.entry, sig.stop_loss))
    exit_price = sig.take_profit if sig.outcome is TradeOutcome.WIN else sig.stop_loss
    pnl = profit(sig.symbol, sig.direction, lot, sig.entry, exit_price)

    p.trades = n
    p.max_lot = max(p.max_lot, lot)
    if p.balance > 0:
        p.max_exposure_pct = max(p.max_exposure_pct, risk / p.balance * 100)
    p.balance += pnl
    p.wins += sig.outcome is TradeOutcome.WIN
    p.losses += sig.outcome is TradeOutcome.LOSS
    p.peak_equity = max(p.peak_equity, p.balance)
    drawdown = p.peak_equity - p.balance
    p.max_drawdown = max(p.max_drawdown, drawdown)
    if p.peak_equity > 0:
        p.max_drawdown_pct = max(p.max_drawdown_pct, drawdown / p.peak_equity * 100)
    p.states[sig.symbol] = settle(state, sig.outcome)
    if p.balance <= 0:
        _fail(p, n, "Virtual account depleted")


def _fail(p: ShadowPortfolio, trade_number: int, reason: str) -> None:
    p.status = ShadowStatus.SHADOW_FAILED
    p.failed_at_trade, p.failure_reason = trade_number, reason


def replay(
    signals: list[ShadowSignal],
    *,
    starting_balance: Decimal,
    profit: ProfitFn,
    margin: MarginFn,
    max_policy: VolumeMaxPolicy,
) -> dict[RiskMode, ShadowPortfolio]:
    """§67: the same chronological signals through all six methods, independently."""
    portfolios = new_portfolios(starting_balance)
    for sig in signals:
        for p in portfolios.values():
            apply_signal(p, sig, profit=profit, margin=margin, max_policy=max_policy)
    return portfolios
