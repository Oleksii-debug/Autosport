"""Seal proposal-risk precommit and execution-evidence public dispatch.

The owning modules exact-fence their upstream economic/scientific roots. This package
composition closes the remaining mutable module surfaces before callers receive the
public package: precommit issuer/resolver dispatch and the proposal execution-evidence
deriver/helpers. No risk approval, ticket, broker, real-money or state-mutation
authority is introduced here.
"""
from __future__ import annotations

from types import FunctionType

from . import proposal_risk_evaluation_precommit_authority as _authority
from . import proposal_risk_execution_evidence_authority as _execution_authority


def _install_precommit_guard() -> None:
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


def _install_execution_evidence_guard() -> None:
    module = _execution_authority
    error_type = module.ProductProposalRiskExecutionEvidenceError
    canonical_derive = module.derive_product_proposal_risk_execution_evidence
    precommit_type = module.ProductProposalRiskEvaluationPrecommit
    row_type = module.CounterfactualMemberExecutionEvidence
    result_type = module.ProductProposalRiskExecutionEvidence

    if type(canonical_derive) is not FunctionType:
        raise RuntimeError(
            "proposal risk execution evidence canonical dispatch is unavailable"
        )

    helper_names = (
        "_text",
        "_sha",
        "_instant",
        "_decimal",
        "_decimal_text",
        "_canonical_json",
        "_digest",
        "_require_estimator_dispatch",
        "_evidence_payload",
        "_mint",
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
            "proposal risk execution evidence helper dispatch is unavailable"
        )
    derive_code = canonical_derive.__code__

    def require_surface() -> None:
        if module.ProductProposalRiskEvaluationPrecommit is not precommit_type:
            raise error_type(
                "proposal risk execution evidence precommit type was rebound"
            )
        if module.CounterfactualMemberExecutionEvidence is not row_type:
            raise error_type(
                "proposal risk execution evidence row type was rebound"
            )
        if module.ProductProposalRiskExecutionEvidence is not result_type:
            raise error_type(
                "proposal risk execution evidence result type was rebound"
            )
        for name, expected, code in helper_witnesses:
            current = getattr(module, name, None)
            if current is not expected or getattr(current, "__code__", None) is not code:
                raise error_type(
                    f"proposal risk execution evidence helper {name} changed"
                )
        if canonical_derive.__code__ is not derive_code:
            raise error_type(
                "proposal risk execution evidence derivation implementation changed"
            )

    def sealed_derive_product_proposal_risk_execution_evidence(
        precommit,
        rows,
        *,
        evaluated_at,
    ):
        if (
            module.derive_product_proposal_risk_execution_evidence
            is not sealed_derive_product_proposal_risk_execution_evidence
        ):
            raise error_type(
                "proposal risk execution evidence public derivation was rebound"
            )
        require_surface()
        result = canonical_derive(
            precommit,
            rows,
            evaluated_at=evaluated_at,
        )
        require_surface()
        if type(result) is not result_type:
            raise error_type(
                "proposal risk execution evidence returned non-canonical result"
            )
        return result

    sealed_derive_product_proposal_risk_execution_evidence._autosport_dispatch_sealed = True
    module.derive_product_proposal_risk_execution_evidence = (
        sealed_derive_product_proposal_risk_execution_evidence
    )


_install_precommit_guard()
_install_execution_evidence_guard()
del _install_precommit_guard
del _install_execution_evidence_guard
del _authority
del _execution_authority
