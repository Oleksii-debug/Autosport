"""Seal proposal/science precommit public dispatch against module rebinding.

The owning authority module already exact-fences its upstream target, scientific,
ledger, goal and policy roots. This composition closes the remaining public seam:
callers must not be able to rebind the module-level dispatch guard or one of the
internal join/build helpers and then invoke an otherwise canonical public issuer or
resolver. No scientific result, risk bound, ticket permission or money authority is
introduced here.
"""
from __future__ import annotations

from types import FunctionType

from . import proposal_risk_evaluation_precommit_authority as _authority


def _install_guard() -> None:
    module = _authority
    error_type = module.ProductProposalRiskEvaluationPrecommitError
    canonical_issue = module.issue_product_proposal_risk_evaluation_precommit
    canonical_resolve = module.resolve_product_proposal_risk_evaluation_precommit
    canonical_require = module._require_dispatch

    if (
        type(canonical_issue) is not FunctionType
        or type(canonical_resolve) is not FunctionType
        or type(canonical_require) is not FunctionType
    ):
        raise RuntimeError(
            "proposal risk evaluation precommit canonical dispatch is unavailable"
        )

    helper_names = (
        "_workspace_path",
        "_sha",
        "_decimal_text",
        "_instant",
        "_canonical_json",
        "_digest",
        "_current_economic_state",
        "_resolve_inputs",
        "_material",
        "_build",
    )
    helper_witnesses: tuple[tuple[str, object, object], ...] = tuple(
        (
            name,
            getattr(module, name),
            getattr(getattr(module, name), "__code__", None),
        )
        for name in helper_names
    )
    if any(code is None for _, _, code in helper_witnesses):
        raise RuntimeError(
            "proposal risk evaluation precommit helper dispatch is unavailable"
        )

    issue_code = canonical_issue.__code__
    resolve_code = canonical_resolve.__code__
    require_code = canonical_require.__code__

    def require_surface() -> None:
        if (
            module._require_dispatch is not canonical_require
            or canonical_require.__code__ is not require_code
        ):
            raise error_type(
                "proposal risk evaluation precommit dispatch guard root changed"
            )
        for name, expected, code in helper_witnesses:
            current = getattr(module, name, None)
            if current is not expected or getattr(current, "__code__", None) is not code:
                raise error_type(
                    f"proposal risk evaluation precommit helper {name} changed"
                )
        if canonical_issue.__code__ is not issue_code:
            raise error_type(
                "proposal risk evaluation precommit issuer implementation changed"
            )
        if canonical_resolve.__code__ is not resolve_code:
            raise error_type(
                "proposal risk evaluation precommit resolver implementation changed"
            )

    def sealed_issue_product_proposal_risk_evaluation_precommit(
        workspace,
        *,
        target_sha256,
        membership,
        registry_path,
        sampling_manifest_json,
        authority_root=None,
    ):
        if (
            module.issue_product_proposal_risk_evaluation_precommit
            is not sealed_issue_product_proposal_risk_evaluation_precommit
        ):
            raise error_type(
                "proposal risk evaluation precommit public issuer was rebound"
            )
        require_surface()
        result = canonical_issue(
            workspace,
            target_sha256=target_sha256,
            membership=membership,
            registry_path=registry_path,
            sampling_manifest_json=sampling_manifest_json,
            authority_root=authority_root,
        )
        require_surface()
        return result

    def sealed_resolve_product_proposal_risk_evaluation_precommit(
        workspace,
        *,
        binding_sha256,
        target_sha256,
        membership,
        registry_path,
        sampling_manifest_json,
        authority_root=None,
    ):
        if (
            module.resolve_product_proposal_risk_evaluation_precommit
            is not sealed_resolve_product_proposal_risk_evaluation_precommit
        ):
            raise error_type(
                "proposal risk evaluation precommit public resolver was rebound"
            )
        require_surface()
        result = canonical_resolve(
            workspace,
            binding_sha256=binding_sha256,
            target_sha256=target_sha256,
            membership=membership,
            registry_path=registry_path,
            sampling_manifest_json=sampling_manifest_json,
            authority_root=authority_root,
        )
        require_surface()
        return result

    sealed_issue_product_proposal_risk_evaluation_precommit._autosport_dispatch_sealed = True
    sealed_resolve_product_proposal_risk_evaluation_precommit._autosport_dispatch_sealed = True
    module.issue_product_proposal_risk_evaluation_precommit = (
        sealed_issue_product_proposal_risk_evaluation_precommit
    )
    module.resolve_product_proposal_risk_evaluation_precommit = (
        sealed_resolve_product_proposal_risk_evaluation_precommit
    )


_install_guard()
del _install_guard
del _authority
