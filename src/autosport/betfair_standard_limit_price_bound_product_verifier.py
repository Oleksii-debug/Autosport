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
    canonical_profile_workspace = canonical_profile_type.workspace
    canonical_profile_error = _runtime_profile.TrustedRuntimeCodeProfileError
    profile_field_descriptors = tuple(
        (name, getattr(canonical_profile_type, name))
        for name in (
            "profile_id",
            "operator_source_id",
            "factory_spec",
            "provider_source_id",
            "workspace",
            "trust_boundary",
        )
    )
    profile_evidence_getter = canonical_profile_type.evidence_sha256.fget
    profile_evidence_getter_code = profile_evidence_getter.__code__
    canonical_store_type = SupervisedPlanIssuanceStore
    canonical_store_init = canonical_store_type.__init__
    canonical_store_init_code = canonical_store_init.__code__
    canonical_ledger_type = RealExecutionLedger
    canonical_ledger_init = canonical_ledger_type.__init__
    canonical_ledger_init_code = canonical_ledger_init.__code__
    path_factory = Path
    canonical_path_type = type(Path("."))
    object_new = object.__new__

    def authority_graph_unchanged() -> bool:
        return (
            _verifier_module.verify_betfair_standard_limit_price_bound
            is canonical_verify
            and canonical_verify.__code__ is canonical_verify_code
            and _runtime_profile.require_authoritative_trusted_runtime_code_profile
            is canonical_require_runtime
            and canonical_require_runtime.__code__ is canonical_require_runtime_code
            and _runtime_profile.TrustedRuntimeCodeProfile is canonical_profile_type
            and canonical_profile_type.workspace is canonical_profile_workspace
            and _runtime_profile.TrustedRuntimeCodeProfileError is canonical_profile_error
            and all(
                getattr(canonical_profile_type, name, None) is descriptor
                for name, descriptor in profile_field_descriptors
            )
            and canonical_profile_type.evidence_sha256.fget is profile_evidence_getter
            and profile_evidence_getter.__code__ is profile_evidence_getter_code
        )

    def reopen_graph_unchanged() -> bool:
        return (
            canonical_store_type.__init__ is canonical_store_init
            and canonical_store_init.__code__ is canonical_store_init_code
            and canonical_ledger_type.__init__ is canonical_ledger_init
            and canonical_ledger_init.__code__ is canonical_ledger_init_code
        )

    def exact_instance_field(value: object, field: str, owner: str):
        try:
            state = object.__getattribute__(value, "__dict__")
        except (AttributeError, TypeError) as exc:
            raise BetfairStandardLimitPriceBoundError(
                f"{owner} instance state is unavailable"
            ) from exc
        if type(state) is not dict or field not in state:
            raise BetfairStandardLimitPriceBoundError(
                f"{owner} instance state is incomplete"
            )
        return state[field]

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
        if not authority_graph_unchanged():
            raise BetfairStandardLimitPriceBoundError(
                "canonical Betfair product verifier authority changed"
            )
        profile_workspace = object.__getattribute__(profile, "workspace")
        if type(profile_workspace) is not str:
            raise BetfairStandardLimitPriceBoundError(
                "canonical product runtime workspace is invalid"
            )
        try:
            workspace = path_factory(profile_workspace).resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise BetfairStandardLimitPriceBoundError(
                "canonical product runtime workspace is invalid"
            ) from exc
        if str(workspace) != profile_workspace:
            raise BetfairStandardLimitPriceBoundError(
                "canonical product runtime workspace is invalid"
            )

        if type(issuance_store) is not canonical_store_type:
            raise BetfairStandardLimitPriceBoundError(
                "product issuance store is not canonical"
            )
        store_workspace_value = exact_instance_field(
            issuance_store,
            "workspace",
            "product issuance store",
        )
        if type(store_workspace_value) is not canonical_path_type:
            raise BetfairStandardLimitPriceBoundError(
                "product issuance store workspace handle is not canonical"
            )
        try:
            store_workspace = store_workspace_value.resolve(strict=False)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise BetfairStandardLimitPriceBoundError(
                "product issuance store is outside the active runtime workspace"
            ) from exc
        if store_workspace != workspace:
            raise BetfairStandardLimitPriceBoundError(
                "product issuance store is outside the active runtime workspace"
            )
        if exact_instance_field(
            issuance_store,
            "authority_root",
            "product issuance store",
        ) is not None:
            raise BetfairStandardLimitPriceBoundError(
                "product issuance store must use the product-selected authority root"
            )

        if type(ledger) is not canonical_ledger_type:
            raise BetfairStandardLimitPriceBoundError(
                "execution ledger is not canonical"
            )
        ledger_path_value = exact_instance_field(
            ledger,
            "path",
            "execution ledger",
        )
        if type(ledger_path_value) is not canonical_path_type:
            raise BetfairStandardLimitPriceBoundError(
                "execution ledger path handle is not canonical"
            )
        try:
            ledger_path = ledger_path_value.resolve(strict=False)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise BetfairStandardLimitPriceBoundError(
                "execution ledger is outside the active runtime workspace"
            ) from exc
        if ledger_path.parent != workspace:
            raise BetfairStandardLimitPriceBoundError(
                "execution ledger is outside the active runtime workspace"
            )

        # Re-open through canonical classes instead of trusting mutable caller
        # object state. Witness constructor authority before invoking either
        # constructor so hostile mutation fails before attacker code executes.
        if not reopen_graph_unchanged():
            raise BetfairStandardLimitPriceBoundError(
                "canonical Betfair product verifier reopen authority changed"
            )
        canonical_store = object_new(canonical_store_type)
        canonical_store_init(canonical_store, workspace)
        canonical_ledger = object_new(canonical_ledger_type)
        canonical_ledger_init(canonical_ledger, ledger_path)
        if not reopen_graph_unchanged():
            raise BetfairStandardLimitPriceBoundError(
                "canonical Betfair product verifier reopen authority changed"
            )
        expected_issuance_directory = workspace / "supervised-plan-issuance"
        if (
            type(
                exact_instance_field(
                    canonical_store,
                    "workspace",
                    "canonical product issuance store",
                )
            )
            is not canonical_path_type
            or exact_instance_field(
                canonical_store,
                "workspace",
                "canonical product issuance store",
            )
            != workspace
            or exact_instance_field(
                canonical_store,
                "authority_root",
                "canonical product issuance store",
            )
            is not None
            or type(
                exact_instance_field(
                    canonical_store,
                    "directory",
                    "canonical product issuance store",
                )
            )
            is not canonical_path_type
            or exact_instance_field(
                canonical_store,
                "directory",
                "canonical product issuance store",
            )
            != expected_issuance_directory
            or type(
                exact_instance_field(
                    canonical_ledger,
                    "path",
                    "canonical execution ledger",
                )
            )
            is not canonical_path_type
            or exact_instance_field(
                canonical_ledger,
                "path",
                "canonical execution ledger",
            )
            != ledger_path
        ):
            raise BetfairStandardLimitPriceBoundError(
                "canonical Betfair product verifier reopen state changed"
            )

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
