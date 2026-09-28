from __future__ import annotations

"""Compatibility product name for the canonical issuance-backed LIMIT verifier.

The compatibility facade must not become a second mutable dispatch seam above the
canonical verifier.  Capture the exact verifier once, witness its executable and
module binding at each call, and delegate only through that captured authority.
"""

from . import betfair_standard_limit_price_bound_verifier as _verifier_module
from .betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
)
from .real_execution_ledger import RealExecutionLedger
from .supervised_plan_issuance import SupervisedPlanIssuanceStore


# Compatibility/debug alias only.  The product facade below does not late-dispatch
# through this mutable module global.
verify_betfair_standard_limit_price_bound = (
    _verifier_module.verify_betfair_standard_limit_price_bound
)


def _build_product_entrypoint():
    canonical_verify = _verifier_module.verify_betfair_standard_limit_price_bound
    canonical_verify_code = canonical_verify.__code__

    def verify_product_betfair_standard_limit_price_bound(
        *,
        evidence: BetfairStandardLimitPriceBoundEvidence,
        ledger: RealExecutionLedger,
        issuance_store: SupervisedPlanIssuanceStore,
        execution_plan_id: str,
        action_id: str,
    ) -> BetfairStandardLimitPriceBoundEvidence:
        """Verify through the exact canonical durable product-issuance verifier."""

        if (
            _verifier_module.verify_betfair_standard_limit_price_bound
            is not canonical_verify
            or canonical_verify.__code__ is not canonical_verify_code
        ):
            raise BetfairStandardLimitPriceBoundError(
                "canonical Betfair product verifier authority changed"
            )
        result = canonical_verify(
            evidence=evidence,
            ledger=ledger,
            issuance_store=issuance_store,
            execution_plan_id=execution_plan_id,
            action_id=action_id,
        )
        if (
            _verifier_module.verify_betfair_standard_limit_price_bound
            is not canonical_verify
            or canonical_verify.__code__ is not canonical_verify_code
        ):
            raise BetfairStandardLimitPriceBoundError(
                "canonical Betfair product verifier authority changed"
            )
        return result

    verify_product_betfair_standard_limit_price_bound.__name__ = (
        "verify_product_betfair_standard_limit_price_bound"
    )
    verify_product_betfair_standard_limit_price_bound.__qualname__ = (
        "verify_product_betfair_standard_limit_price_bound"
    )
    return verify_product_betfair_standard_limit_price_bound


verify_product_betfair_standard_limit_price_bound = _build_product_entrypoint()
del _build_product_entrypoint
