"""Weekly / monthly reports (README §47–§50) built from the journal (settled trades only)."""

from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from plough_backer.analytics.metrics import Metrics, TradeRecord, breakdown, compute
from plough_backer.enums import AuditEventType, RiskMode, SignalState, TradeOutcome
from plough_backer.formatting import lot, mode_label, money
from plough_backer.persistence.models import AuditEvent, Signal, Trade
from plough_backer.shadow.engine import ShadowPortfolio


def previous_week(today: date, tz: str) -> tuple[datetime, datetime]:
    """Monday 00:00 to next Monday 00:00 of the last full week, in the report timezone."""
    monday = today - timedelta(days=today.weekday() + 7)
    start = datetime.combine(monday, time(), ZoneInfo(tz))
    return start, start + timedelta(days=7)


def previous_month(today: date, tz: str) -> tuple[datetime, datetime]:
    first_this = today.replace(day=1)
    first_prev = (first_this - timedelta(days=1)).replace(day=1)
    zone = ZoneInfo(tz)
    return datetime.combine(first_prev, time(), zone), datetime.combine(first_this, time(), zone)


def load_trades(s: Session, start: datetime, end: datetime) -> tuple[list[TradeRecord], Decimal]:
    """Settled trades closed in [start, end) and the opening balance (first balance_before)."""
    rows = s.scalars(
        select(Trade)
        .where(Trade.settled_at.is_not(None), Trade.closed_at >= start, Trade.closed_at < end)
        .order_by(Trade.closed_at)
    ).all()
    records = [
        TradeRecord(
            closed_at=t.closed_at or start,
            source_id=t.telegram_source_id,
            symbol=t.symbol_mt5,
            result=TradeOutcome(str(t.result)),  # settled rows always have a result
            net_profit=t.net_profit or Decimal(0),
            r_multiple=t.realized_r_multiple,
            executed_lot=t.executed_lot,
            estimated_risk=t.estimated_risk,
            equity_risk_percent=t.equity_risk_percent,
        )
        for t in rows
    ]
    opening = rows[0].balance_before if rows else Decimal(0)
    return records, opening


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:+.2f}%" if value < 0 or value > 0 else "0.00%"


def _num(value: Decimal | None, places: int = 2) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def _core(
    title: str,
    start: datetime,
    end: datetime,
    m: Metrics,
    mode: RiskMode,
    by_symbol: dict[str, Metrics],
) -> list[str]:
    last_day = (end - timedelta(days=1)).date()
    lines = [
        "🚜 PLOUGH BACKER",
        title,
        "",
        "Period:",
        f"{start:%d %b} – {last_day:%d %b %Y}",
        "",
        "ACCOUNT",
        "",
        f"Opening Balance: {money(m.starting_balance)}",
        f"Closing Balance: {money(m.ending_balance)}",
        "",
        f"Net P/L: {money(m.net_pl)}",
        f"Return:  {_pct(m.return_pct)}",
        "",
        "TRADES",
        "",
        f"Total: {m.total_trades}",
        f"Wins:  {m.wins}",
        f"Losses: {m.losses}",
        f"Breakeven: {m.breakeven}",
        "",
        f"Win Rate: {_num(m.win_rate)}%",
        "",
        f"Gross Profit: {money(m.gross_profit)}",
        f"Gross Loss:   {money(m.gross_loss)}",
        f"Profit Factor:{_num(m.profit_factor)}",
        "",
        f"Average R: {_num(m.average_r)}",
        f"Expectancy:{_num(m.expectancy_r)}R",
        "",
        "RISK",
        "",
        "Method:",
        mode_label(mode),
        "",
        f"Highest Lot:     {lot(m.max_lot)}",
        f"Average Lot:     {lot(m.average_lot)}",
        "",
        f"Highest $ Risk:  {money(m.max_risk)}",
        f"Highest Equity Exposure: {_num(m.max_exposure_pct, 1)}%",
        "",
        "DRAWDOWN",
        "",
        f"Max DD:       {money(m.max_drawdown)}",
        f"Max DD:       {_num(m.max_drawdown_pct)}%",
        f"Longest W:    {m.longest_win_streak}",
        f"Longest L:    {m.longest_loss_streak}",
        "",
        "INSTRUMENTS",
        "",
    ]
    for symbol, sm in by_symbol.items():
        lines += [f"{symbol}:", f"{sm.total_trades} trades | {sm.wins}W / {sm.losses}L", ""]
    return lines


def shadow_section(portfolios: dict[RiskMode, ShadowPortfolio]) -> list[str]:
    """§48: deterministic results only, no recommendation."""
    lines = ["🧪 SHADOW LAB", "", "Same signals replayed through all methods:", ""]
    for mode, p in portfolios.items():
        lines.append(mode_label(mode))
        if p.failed_at_trade is not None:
            lines += [f"❌ Failed on Trade {p.failed_at_trade}", f"Reason: {p.failure_reason}"]
        else:
            lines += [
                f"Virtual Balance: {money(p.balance)}",
                f"Return: {_pct(p.return_pct)}",
                f"Max DD: {money(p.max_drawdown)} ({_num(p.max_drawdown_pct, 1)}%)",
            ]
        lines.append("")
    return lines


def weekly_report(
    s: Session,
    start: datetime,
    end: datetime,
    mode: RiskMode,
    shadow: dict[RiskMode, ShadowPortfolio] | None = None,
) -> str:
    trades, opening = load_trades(s, start, end)
    m = compute(trades, opening)
    lines = _core("WEEKLY REPORT", start, end, m, mode, breakdown(trades, lambda t: t.symbol))
    if shadow:
        lines += shadow_section(shadow)
    return "\n".join(lines).rstrip()


def _signal_counts(s: Session, start: datetime, end: datetime) -> Counter[str]:
    rows = s.execute(
        select(Signal.state, func.count())
        .where(Signal.received_at >= start, Signal.received_at < end)
        .group_by(Signal.state)
    ).all()
    return Counter({state: n for state, n in rows})


def monthly_report(
    s: Session,
    start: datetime,
    end: datetime,
    mode: RiskMode,
    shadow: dict[RiskMode, ShadowPortfolio] | None = None,
) -> str:
    """§49: everything weekly has, plus the monthly-only sections."""
    trades, opening = load_trades(s, start, end)
    m = compute(trades, opening)
    lines = _core("MONTHLY REPORT", start, end, m, mode, breakdown(trades, lambda t: t.symbol))
    lines += ["BY SOURCE", ""]
    for source, sm in breakdown(trades, lambda t: t.source_id).items():
        lines.append(
            f"{source}: {sm.total_trades} trades | {sm.wins}W / {sm.losses}L | "
            f"{money(sm.net_pl)} | {_num(sm.total_r)}R"
        )
    lines += ["", "BY WEEK", ""]
    week_start = start - timedelta(days=start.weekday())
    while week_start < end:
        week_end = week_start + timedelta(days=7)
        wk = [t for t in trades if week_start <= t.closed_at < week_end]
        lines.append(
            f"{week_start:%d %b}: {len(wk)} trades | "
            f"{money(sum((t.net_profit for t in wk), Decimal(0)))}"
        )
        week_start = week_end

    counts = _signal_counts(s, start, end)
    latencies = [
        (t.broker_response_at - t.telegram_received_at).total_seconds()
        for t in s.scalars(
            select(Trade).where(
                Trade.telegram_received_at >= start,
                Trade.telegram_received_at < end,
                Trade.broker_response_at.is_not(None),
            )
        )
        if t.broker_response_at is not None
    ]
    incidents = _audit_count(s, AuditEventType.MT5_DISCONNECTED, start, end)
    # Both kinds of duplicate (re-delivered message, repeated fingerprint) are audited.
    duplicates = _audit_count(s, AuditEventType.SIGNAL_DUPLICATE, start, end)
    avg_latency = f"{sum(latencies) / len(latencies):.2f}s" if latencies else "—"
    lines += [
        "",
        "OPERATIONS",
        "",
        f"Total R:                 {_num(m.total_r)}",
        f"Largest lot reached:     {lot(m.max_lot)}",
        f"Largest $ exposure:      {money(m.max_risk)}",
        f"Avg execution latency:   {avg_latency}",
        f"Broker rejections:       {counts[SignalState.BROKER_REJECTED]}",
        f"Parser rejections:       "
        f"{counts[SignalState.REJECTED_PARSE] + counts[SignalState.REJECTED_INVALID_SIGNAL]}",
        f"Duplicates:              {duplicates}",
        f"Equity Lock blocks:      {counts[SignalState.BLOCKED_EQUITY_LOCK]}",
        f"MT5 connectivity incidents: {incidents}",
        "",
    ]
    if shadow:
        lines += shadow_section(shadow)
    return "\n".join(lines).rstrip()


def _audit_count(s: Session, kind: AuditEventType, start: datetime, end: datetime) -> int:
    return (
        s.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == kind, AuditEvent.at >= start, AuditEvent.at < end)
        )
        or 0
    )
