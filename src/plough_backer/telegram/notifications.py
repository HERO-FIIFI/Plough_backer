"""Telegram message text (README §10, §20, §25, §40, §57). Pure formatting, no I/O."""

from plough_backer.enums import RiskMode, SignalState
from plough_backer.formatting import lot, mode_label, money
from plough_backer.trading.executor import EditOutcome, Outcome
from plough_backer.trading.settlement import Settlement

__all__ = ["lot", "mode_label", "money"]

_REASON_TEXT = {
    "STOP_LOSS_MISSING": "Stop loss could not be determined.",
    "TP2_MISSING": "TP2 missing (Gold policy: TP2, missing TP2 → reject).",
    "TP_MISSING": "Configured take profit missing.",
    "TP_POLICY_NOT_CONFIGURED": "No take-profit policy configured for this symbol.",
    "MALFORMED_NUMBER": "A price could not be read reliably.",
    "UNSUPPORTED_SYMBOL": "Symbol is not configured.",
    "AMBIGUOUS_SIGNAL": "Message contains conflicting instructions.",
    "AMBIGUOUS_ENTRY": "Entry price is ambiguous.",
    "ENTRY_MISSING": "Pending order has no price.",
    "UNRECOGNIZED_FORMAT": "Message format not recognised.",
    "UNKNOWN_SOURCE": "Source is not configured.",
}


def trade_closed(st: Settlement, mode: RiskMode) -> str:
    """§41 closure notification."""
    icon = {"WIN": "✅", "LOSS": "❌"}.get(st.outcome, "⚠️")
    r = f"{st.r_multiple:+.2f}R" if st.r_multiple is not None else "—"
    sign = "+" if st.net_profit >= 0 else "-"
    lines = [
        f"{icon} TRADE CLOSED — {st.outcome}",
        "",
        f"{st.symbol} {st.direction}",
        f"Ticket: #{st.position_id}",
        "",
        f"P/L:       {sign}{money(abs(st.net_profit))}",
        f"Result:    {st.outcome}",
        f"R Multiple:{r}",
        "",
        "Mode:",
        mode_label(mode),
        "",
    ]
    if st.previous_lot is None:
        lines.append("Progression unchanged.")
    else:
        lines += [
            f"Previous theoretical lot: {lot(st.previous_lot)}",
            f"Next theoretical lot:     {lot(st.next_lot)}",
        ]
    if st.needs_review:
        lines += ["", "🔎 Flagged for review."]
    return "\n".join(lines)


def trade_executed(out: Outcome, source_name: str, mode: RiskMode) -> str:
    s, r, res = out.signal, out.risk, out.result
    assert s is not None
    assert r is not None
    assert res is not None
    rr = f"1:{r.reward_risk_ratio:.2f}" if r.reward_risk_ratio is not None else "—"
    pct = f"{r.equity_risk_percent:.1f}%" if r.equity_risk_percent is not None else "—"
    entry = res.price if res.price is not None else s.entry
    lines = [
        "🚜 TRADE EXECUTED",
        "",
        f"{s.symbol_mt5} {s.direction}"
        + ("" if not s.order_type.is_pending else f" ({s.order_type})"),
        "",
        f"Source: {source_name}",
        f"Entry: {entry}",
        f"SL:    {s.stop_loss}",
        f"TP:    {s.selected_take_profit}",
        "",
        "Method:",
        mode_label(mode),
        "",
        f"Theoretical Lot: {lot(out.theoretical_lot)}",
        f"Executed Lot:    {out.executed_lot}",
        "",
        f"Risk to SL:      {money(r.risk_to_sl)}",
        f"TP Reward:       {money(r.reward_to_tp)}",
        f"Actual RR:       {rr}",
        f"Equity Risk:     {pct}",
        "",
        f"MT5 Ticket: #{res.order_id}",
        f"Signal: {out.signal_id}",
    ]
    if out.volume_capped:
        lines.insert(14, "⚠️ Capped at broker maximum volume")
    return "\n".join(lines)


def signal_not_executed(out: Outcome, source_name: str) -> str:
    symbol = out.signal.symbol_mt5 if out.signal else "—"
    reason = _REASON_TEXT.get(out.reason or "", out.reason or "")
    return (
        f"⚠️ SIGNAL NOT EXECUTED\n\nSource: {source_name}\nSymbol: {symbol}\n"
        f"Reason: {reason}\n\nMessage recorded for review."
    )


def equity_lock_blocked(out: Outcome) -> str:
    lock, s = out.lock, out.signal
    assert lock is not None
    assert s is not None
    return (
        "🔒 TRADE BLOCKED — EQUITY LOCK\n\n"
        f"Symbol: {s.symbol_mt5}\nProposed Lot: {out.executed_lot}\n\n"
        f"Current Equity:   {money(lock.current_equity)}\n"
        f"Risk to SL:       {money(lock.risk_to_sl)}\n"
        f"Projected Equity: {money(lock.projected_equity)}\n"
        f"Protected Equity: {money(lock.protected_equity)}\n\n"
        "Signal recorded.\nProgression unchanged."
    )


def message_edited(edit: EditOutcome) -> str:
    return (
        "⚠️ SOURCE MESSAGE EDITED\n\n"
        f"Signal: {edit.signal_id}\nMT5 Ticket: {edit.mt5_ticket}\n\n"
        "The source message changed after execution.\n"
        "V1 does not automatically modify active positions."
    )


_SKIPPED = {
    SignalState.SKIPPED_PAUSED: "⏸ SIGNAL SKIPPED — PAUSED",
    SignalState.SKIPPED_STOPPED: "⛔ SIGNAL SKIPPED — KILL SWITCH ACTIVE",
    SignalState.SKIPPED_MT5_UNAVAILABLE: "🔴 SIGNAL SKIPPED — MT5 UNAVAILABLE",
    SignalState.BROKER_REJECTED: "❌ BROKER REJECTED ORDER",
    SignalState.EXECUTION_FAILED: "❌ EXECUTION FAILED",
}


def for_outcome(out: Outcome, source_name: str, mode: RiskMode) -> str | None:
    """Pick the notification for an orchestrator outcome. None = nothing to send."""
    if out.state in (SignalState.OPEN, SignalState.EXECUTED):
        return trade_executed(out, source_name, mode)
    if out.state in (SignalState.REJECTED_PARSE, SignalState.REJECTED_INVALID_SIGNAL):
        return signal_not_executed(out, source_name)
    if out.state is SignalState.BLOCKED_EQUITY_LOCK:
        return equity_lock_blocked(out)
    if out.state in _SKIPPED:
        detail = out.extra.get("detail", "")
        signal = f"\n\nSignal: {out.signal_id}" if out.signal_id else ""
        return (
            f"{_SKIPPED[out.state]}{signal}"
            + (f"\n{detail}" if detail else "")
            + ("\nProgression unchanged.")
        )
    # Duplicates and paper sizing are journaled; no push (avoid spam on catch-up replays).
    return None
