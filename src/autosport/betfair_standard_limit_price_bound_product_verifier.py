from __future__ import annotations

"""Compatibility product name for the canonical issuance-backed LIMIT verifier."""

from .betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundEvidence,
)
from .betfair_standard_limit_price_bound_verifier import (
    verify_betfair_standard_limit_price_bound,
)
from .real_execution_ledger import RealExecutionLedger
from .supervised_plan_issuance import SupervisedPlanIssuanceStore


def verify_product_betfair_standard_limit_price_bound(
    *,
    evidence: BetfairStandardLimitPriceBoundEvidence,
    ledger: RealExecutionLedger,
    issuance_store: SupervisedPlanIssuanceStore,
    execution_plan_id: str,
    action_id: str,
) -> BetfairStandardLimitPriceBoundEvidence:
    """Verify through the canonical durable product-issuance trust root."""

    return verify_betfair_standard_limit_price_bound(
        evidence=evidence,
        ledger=ledger,
        issuance_store=issuance_store,
        execution_plan_id=execution_plan_id,
        action_id=action_id,
    )
