from __future__ import annotations

"""Product-facing verification for the canonical issuance-backed LIMIT bound.

The low-level verifier proves durable issuance/request/approval continuity. This
facade additionally binds that proof to the exact currently-running product
workspace. Caller-provided store/ledger objects are treated only as path handles:
their durable state is re-opened through canonical classes before verification.
"""

from pathlib import Path

from . import betfair_standard_limit_price_bound_verifier as _verifier_module
from . import trusted_runtime_code_profile as _runtime_profile
from .betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
)
from .real_execution_ledger import RealExecutionLedger
from .supervised_plan_issuance import SupervisedPlanIssuanceStore


# Compatibility/debug alias only. The product facade below does not late-dispatch
# through this mutable module global.
verify_betfair_standard_limit_price_bound = (
    _verifier_module.verify_betfair_standard_limit_price_bound
)


def _build_product_entrypoint():
    canonical_verify = _verifier_module.verify_betfair_standard_limit_price_bound
    canonical_verify_code = canonical_verify.__code__
    canonical_require_runtime = (
        _runtime_profile.require_authoritative_trusted_runtime_code_profile
    )
    canonical_require_runtime_code = canonical_require_runtime.__code__
    canonical_profile_type = _runtime_profile.TrustedRuntimeCodeProfile
    canonical_profile_error = _runtime_profile.TrustedRuntimeCodeProfileError
    canonical_store_type = SupervisedPlanIssuanceStore
    canonical_ledger_type = RealExecutionLedger

    def authority_graph_unchanged() -> bool:
        return (
            _verifier_module.verify_betfair_standard_limit_price_bound
            is canonical_verify
            and canonical_verify.__code__ is canonical_verify_code
            and _runtime_profile.require_authoritative_trusted_runtime_code_profile
            is canonical_require_runtime
            and canonical_require_runtime.__code__ is canonical_require_runtime_code
            and _runtime_profile.TrustedRuntimeCodeProfile is canonical_profile_type
            and _runtime_profile.TrustedRuntimeCodeProfileError is canonical_profile_error
        )

    def require_runtime(
        runtime_profile: object,
        *,
        workspace: Path | None = None,
    ):
        if not authority_graph_unchanged():
            raise BetfairStandardLimitPriceBoundError(
                "canonical Betfair product verifier authority changed"
            )
        if type(runtime_profile) is not canonical_profile_type:
            raise BetfairStandardLimitPriceBoundError(
                "canonical product runtime authority is missing or changed"
            )
        try:
            resolved = canonical_require_runtime(
                runtime_profile,
                workspace=workspace,
            )
        except canonical_profile_error as exc:
            raise BetfairStandardLimitPriceBoundError(
                "canonical product runtime authority is missing or changed"
            ) from exc
        if resolved is not runtime_profile:
            raise BetfairStandardLimitPriceBoundError(
                "canonical product runtime authority is missing or changed"
            )
        return resolved

    def verify_product_betfair_standard_limit_price_bound(
        *,
        evidence: BetfairStandardLimitPriceBoundEvidence,
        ledger: RealExecutionLedger,
        issuance_store: SupervisedPlanIssuanceStore,
        runtime_profile: _runtime_profile.TrustedRuntimeCodeProfile,
        execution_plan_id: str,
        action_id: str,
    ) -> BetfairStandardLimitPriceBoundEvidence:
        """Verify durable state rooted in the exact active product workspace.

        TrustedRuntimeCodeProfile is a workspace/code-origin prerequisite only;
        it does not grant provider-write or real-money authority. There is
        intentionally no fixed RealExecutionLedger basename: current product
        contracts permit multiple ledger filenames, but the ledger must live
        directly inside the exact active product workspace.
        """

        profile = require_runtime(runtime_profile)
        try:
            workspace = Path(profile.workspace).resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise BetfairStandardLimitPriceBoundError(
                "canonical product runtime workspace is invalid"
            ) from exc
        if str(workspace) != profile.workspace:
            raise BetfairStandardLimitPriceBoundError(
                "canonical product runtime workspace is invalid"
            )

        if type(issuance_store) is not canonical_store_type:
            raise BetfairStandardLimitPriceBoundError(
                "product issuance store is not canonical"
            )
        try:
            store_workspace = Path(issuance_store.workspace).resolve(strict=False)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise BetfairStandardLimitPriceBoundError(
                "product issuance store is outside the active runtime workspace"
            ) from exc
        if store_workspace != workspace:
            raise BetfairStandardLimitPriceBoundError(
                "product issuance store is outside the active runtime workspace"
            )

        if type(ledger) is not canonical_ledger_type:
            raise BetfairStandardLimitPriceBoundError(
                "execution ledger is not canonical"
            )
        try:
            ledger_path = Path(ledger.path).resolve(strict=False)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise BetfairStandardLimitPriceBoundError(
                "execution ledger is outside the active runtime workspace"
            ) from exc
        if ledger_path.parent != workspace:
            raise BetfairStandardLimitPriceBoundError(
                "execution ledger is outside the active runtime workspace"
            )

        # Re-open through canonical classes instead of trusting mutable caller
        # object state. The issuance authority root is deliberately re-selected
        # by the product from its canonical workspace rather than inherited from
        # the caller-provided handle.
        canonical_store = canonical_store_type(workspace)
        canonical_ledger = canonical_ledger_type(ledger_path)

        # Re-resolve after path canonicalization/re-open and again after the pure
        # verifier. If runtime/profile authority is revoked concurrently, no
        # positive product evidence is returned.
        require_runtime(runtime_profile, workspace=workspace)
        result = canonical_verify(
            evidence=evidence,
            ledger=canonical_ledger,
            issuance_store=canonical_store,
            execution_plan_id=execution_plan_id,
            action_id=action_id,
        )
        require_runtime(runtime_profile, workspace=workspace)
        if not authority_graph_unchanged():
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
