"""Monetary risk of a sized trade (README §18). Display only — never gates execution (§19).

P/L comes from the broker's own calculation via BrokerGateway.calc_profit, so no contract
size, tick value or pip convention is assumed (§13, INV-09).
"""

from dataclasses import dataclass
from decimal import Decimal

from plough_backer.enums import Direction
from plough_backer.trading.gateway import BrokerGateway

_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskAssessment:
    executed_lot: Decimal
    risk_to_sl: Decimal  # money lost if SL is hit
    reward_to_tp: Decimal  # money gained if selected TP is hit
    reward_risk_ratio: Decimal | None  # displayed as 1:x; None when risk is zero
    equity_risk_percent: Decimal | None  # None when equity <= 0


def assess_risk(
    gateway: BrokerGateway,
    *,
    symbol: str,
    direction: Direction,
    executed_lot: Decimal,
    entry_price: Decimal,  # caller decides signal entry vs live tick (Q-06)
    stop_loss: Decimal,
    take_profit: Decimal,
    equity: Decimal,
) -> RiskAssessment:
    risk = abs(gateway.calc_profit(symbol, direction, executed_lot, entry_price, stop_loss))
    reward = abs(gateway.calc_profit(symbol, direction, executed_lot, entry_price, take_profit))
    return RiskAssessment(
        executed_lot=executed_lot,
        risk_to_sl=risk,
        reward_to_tp=reward,
        reward_risk_ratio=reward / risk if risk else None,
        equity_risk_percent=risk / equity * _HUNDRED if equity > 0 else None,
    )
