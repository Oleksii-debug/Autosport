from __future__ import annotations

"""Product trust-root verifier for prospective Betfair LIMIT price evidence.

The lower-level verifier deliberately accepts an already-typed bound so it can be
used at pure decision/projection boundaries. Positive product authority must not
accept that caller value as provenance. This wrapper re-loads the exact bound and
approval from the independent durable supervised-plan issuance authority, then
uses the existing verifier only after that product-owned provenance has been
re-established across restart.
"""

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
    """Verify candidate evidence against restart-stable product issuance authority."""

    if type(issuance_store) is not SupervisedPlanIssuanceStore:
        raise TypeError("issuance_store must be exact SupervisedPlanIssuanceStore")
    issued = issuance_store.load(execution_plan_id)
    return verify_betfair_standard_limit_price_bound(
        evidence=evidence,
        ledger=ledger,
        bound=issued.bound,
        action_id=action_id,
    )
