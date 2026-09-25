"""Take-profit selection (README §12, INV-08). Never substitutes a TP silently."""

from decimal import Decimal

from plough_backer.config import GoldPolicy, SymbolMapping
from plough_backer.enums import AssetClass, MissingTp2Policy, RejectionReason
from plough_backer.exceptions import InvalidSignal


def select_take_profit(
    labelled: dict[int, Decimal], mapping: SymbolMapping, gold: GoldPolicy
) -> Decimal:
    if mapping.asset_class is AssetClass.GOLD:
        wanted = gold.take_profit_policy.tp_number
        if wanted not in labelled:
            # MissingTp2Policy has only REJECT (§12); any fallback needs a spec decision.
            assert gold.missing_tp2_policy is MissingTp2Policy.REJECT
            raise InvalidSignal(RejectionReason.TP2_MISSING, f"TP{wanted} not in signal")
        return labelled[wanted]
    if mapping.take_profit_policy is None:
        raise InvalidSignal(
            RejectionReason.TP_POLICY_NOT_CONFIGURED,
            f"no take_profit_policy configured for {mapping.mt5_symbol} (SPEC_NOTES Q-04)",
        )
    wanted = mapping.take_profit_policy.tp_number
    if wanted not in labelled:
        raise InvalidSignal(RejectionReason.TP_MISSING, f"TP{wanted} not in signal")
    return labelled[wanted]
