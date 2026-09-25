"""Settlement and reconciliation (README §22, §29, §65, Phase 8).

MT5 history is authoritative (INV-14). A trade settles exactly once: `settled_at` is checked
and set inside the same BEGIN IMMEDIATE transaction that saves the progression (§26).
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from plough_backer.enums import AuditEventType, SignalState, TradeOutcome
from plough_backer.persistence import repositories as repo
from plough_backer.persistence.models import Signal, Trade
from plough_backer.risk.progression import settle
from plough_backer.trading.gateway import BrokerGateway, Deal

log = logging.getLogger(__name__)

DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 1, 3
DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO = 4, 5, 6


@dataclass(frozen=True, slots=True, kw_only=True)
class SettlementPolicy:
    """How a closed trade is classified (Q-03). Deliberately no defaults."""

    breakeven_tolerance: Decimal  # |net P/L| <= this -> BREAKEVEN
    manual_close_is_other: bool  # True: closes not by SL/TP/stop-out -> OTHER (+review)


@dataclass(frozen=True, slots=True)
class Settlement:
    trade_id: int
    signal_id: str
    symbol: str
    direction: str
    position_id: int
    outcome: TradeOutcome
    net_profit: Decimal
    r_multiple: Decimal | None
    previous_lot: Decimal | None  # None when progression was not touched
    next_lot: Decimal | None
    needs_review: bool


def classify(net: Decimal, exit_reason: int, policy: SettlementPolicy) -> TradeOutcome:
    if policy.manual_close_is_other and exit_reason not in (
        DEAL_REASON_SL,
        DEAL_REASON_TP,
        DEAL_REASON_SO,
    ):
        return TradeOutcome.OTHER
    if abs(net) <= policy.breakeven_tolerance:
        return TradeOutcome.BREAKEVEN
    return TradeOutcome.WIN if net > 0 else TradeOutcome.LOSS


_CLOSED_STATE = {
    TradeOutcome.WIN: SignalState.CLOSED_WIN,
    TradeOutcome.LOSS: SignalState.CLOSED_LOSS,
    TradeOutcome.BREAKEVEN: SignalState.CLOSED_OTHER,  # no CLOSED_BREAKEVEN in §7 (Q-11)
    TradeOutcome.OTHER: SignalState.CLOSED_OTHER,
}


class Settler:
    def __init__(
        self, *, sessions: sessionmaker[Session], gateway: BrokerGateway, policy: SettlementPolicy
    ) -> None:
        self._sessions = sessions
        self._gateway = gateway
        self._policy = policy

    def reconcile(self) -> list[Settlement]:
        """One reconciliation pass. Safe to run repeatedly and after restarts (§30 steps 8-10)."""
        with self._sessions.begin() as s:
            candidates = s.execute(
                select(Trade.id, Trade.mt5_position_id).where(
                    Trade.settled_at.is_(None), Trade.mt5_position_id.is_not(None)
                )
            ).all()
        if not candidates:
            return []
        still_open = self._gateway.open_position_ids()
        settled = []
        for trade_id, position_id in candidates:
            if position_id in still_open:
                continue
            deals = self._gateway.deals_for_position(position_id)
            exits = [d for d in deals if d.entry in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY)]
            if not exits:
                # §29 "missing positions": not open, no closing deal (yet). Report, don't guess.
                log.warning(
                    "position_missing", extra={"trade_id": trade_id, "position_id": position_id}
                )
                continue
            result = self._settle_one(trade_id, deals, exits)
            if result is not None:
                settled.append(result)
        return settled

    def _settle_one(self, trade_id: int, deals: list[Deal], exits: list[Deal]) -> Settlement | None:
        profit = sum((d.profit for d in deals), Decimal(0))
        swap = sum((d.swap for d in deals), Decimal(0))
        commission = sum((d.commission + d.fee for d in deals), Decimal(0))
        net = profit + swap + commission
        last = max(exits, key=lambda d: (d.time, d.ticket))
        outcome = classify(net, last.reason, self._policy)

        with self._sessions.begin() as s:
            trade = s.get(Trade, trade_id)
            if trade is None or trade.settled_at is not None:
                return None  # already settled by an earlier/concurrent pass (§65)
            r_multiple = net / trade.estimated_risk if trade.estimated_risk else None
            prev_lot = next_lot = None
            review = outcome is TradeOutcome.OTHER
            loaded = repo.load_progression(s, trade.progression_scope_key)
            if loaded is not None and int(loaded[0].mode) == trade.risk_mode:
                state, version = loaded
                new_state = settle(state, outcome)
                if new_state != state:  # BREAKEVEN/OTHER: no change, so no write (§22)
                    repo.save_progression(
                        s, trade.progression_scope_key, new_state, expected_version=version
                    )
                prev_lot, next_lot = state.theoretical_lot, new_state.theoretical_lot
            else:
                # Mode changed (or state missing) since this trade opened: applying its result
                # to a different method is undefined (Q-22). Record it, touch nothing, flag.
                review = True

            trade.closed_at, trade.close_price = last.time, last.price
            trade.realized_profit, trade.realized_swap = profit, swap
            trade.realized_commission, trade.net_profit = commission, net
            trade.result, trade.realized_r_multiple = outcome, r_multiple
            trade.needs_review = review
            trade.settled_at = last.time
            signal = s.get(Signal, trade.signal_pk)
            assert signal is not None
            repo.transition_signal(s, signal, SignalState.CLOSED)
            repo.transition_signal(s, signal, _CLOSED_STATE[outcome])
            repo.append_audit(
                s,
                AuditEventType.TRADE_SETTLED,
                signal_id=trade.signal_id,
                outcome=outcome,
                net_profit=net,
                previous_lot=prev_lot,
                next_lot=next_lot,
            )
            return Settlement(
                trade_id=trade.id,
                signal_id=trade.signal_id,
                symbol=trade.symbol_mt5,
                direction=trade.direction,
                position_id=last.position_id,
                outcome=outcome,
                net_profit=net,
                r_multiple=r_multiple,
                previous_lot=prev_lot,
                next_lot=next_lot,
                needs_review=review,
            )
