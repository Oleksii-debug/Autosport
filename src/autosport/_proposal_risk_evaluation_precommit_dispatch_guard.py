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


class _WriteOnceSlot:
    """Delegate one dataclass slot while rejecting post-construction mutation."""

    __slots__ = ("_slot", "_name")

    def __init__(self, slot, name: str) -> None:
        self._slot = slot
        self._name = name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return self._slot.__get__(instance, owner)

    def __set__(self, instance, value) -> None:
        try:
            self._slot.__get__(instance, type(instance))
        except AttributeError:
            self._slot.__set__(instance, value)
            return
        raise AttributeError(f"{self._name} is write-once product evidence")

    def __delete__(self, instance) -> None:
        raise AttributeError(f"{self._name} is write-once product evidence")


def _seal_write_once_slots(owner, names) -> None:
    for name in names:
        current = owner.__dict__.get(name)
        if isinstance(current, _WriteOnceSlot):
            continue
        if current is None or not hasattr(current, "__get__") or not hasattr(current, "__set__"):
            raise RuntimeError(f"proposal risk product slot {name} is unavailable")
        setattr(owner, name, _WriteOnceSlot(current, name))


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
    canonical_mint = module._mint
    precommit_type = module.ProductProposalRiskEvaluationPrecommit
    row_type = module.CounterfactualMemberExecutionEvidence
    result_type = module.ProductProposalRiskExecutionEvidence

    if type(canonical_derive) is not FunctionType or type(canonical_mint) is not FunctionType:
        raise RuntimeError(
            "proposal risk execution evidence canonical dispatch is unavailable"
        )
    derive_kwdefaults = canonical_derive.__kwdefaults__
    if (
        type(derive_kwdefaults) is not dict
        or set(derive_kwdefaults) != {"_mint_capability"}
    ):
        raise RuntimeError(
            "proposal risk execution evidence mint capability is unavailable"
        )
    canonical_mint_capability = derive_kwdefaults["_mint_capability"]
    canonical_mint_code = canonical_mint.__code__

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
    )
    module_root_names = (
        "_SCHEMA",
        "_BOUND_METHOD",
        "_EXECUTION_SCOPE",
        "_HEX",
        "_MAX_DECIMAL_TEXT",
        "_CP",
        "_CP_CODE",
        "_SOURCE_DIGEST",
        "_SOURCE_DIGEST_CODE",
        "_RESULT_FIELDS",
    )
    module_root_witnesses = tuple(
        (name, getattr(module, name)) for name in module_root_names
    )
    precommit_descriptor_names = (
        "__new__",
        "workspace_instance_id",
        "binding_sha256",
        "target_sha256",
        "target_decision_ts",
        "candidate_vector_sha256",
        "evaluated_stakes",
        "planned_member_ids",
        "membership_outcome_reveal_after",
        "confidence_level",
        "ruin_threshold",
        "proposal_evaluation_scope",
        "binding_identity_proven",
        "proposal_target_counterfactual_execution_proven",
        "risk_upper_bound_for_target",
        "grants_ticket_authority",
        "grants_real_money_authority",
    )
    row_descriptor_names = (
        "__init__",
        "__post_init__",
        "member_id",
        "binding_sha256",
        "target_sha256",
        "candidate_vector_sha256",
        "executed_stakes",
        "execution_engine_sha256",
        "source_sha256",
        "observed_at",
        "starting_equity",
        "minimum_equity",
        "terminal_equity",
        "gross_pnl",
        "costs",
        "net_pnl",
    )
    result_descriptor_names = (
        "__new__",
        "execution_evidence_identity_proven",
        "fixed_n_cohort_complete",
        "statistical_bound_computed",
        "product_execution_provenance_proven",
        "proposal_target_counterfactual_execution_proven",
        "risk_upper_bound_for_target",
        "proposal_target_risk_qualified",
        "grants_risk_approval_authority",
        "grants_ticket_authority",
        "grants_broker_execution_authority",
        "grants_real_money_authority",
        "grants_state_mutation_authority",
    )
    precommit_descriptor_witnesses = tuple(
        (name, precommit_type.__dict__.get(name))
        for name in precommit_descriptor_names
    )
    row_descriptor_witnesses = tuple(
        (name, row_type.__dict__.get(name))
        for name in row_descriptor_names
    )
    result_descriptor_witnesses = tuple(
        (name, result_type.__dict__.get(name))
        for name in result_descriptor_names
    )
    if any(value is None for _, value in precommit_descriptor_witnesses):
        raise RuntimeError(
            "proposal risk execution evidence precommit input/truth descriptors are unavailable"
        )
    if any(value is None for _, value in row_descriptor_witnesses):
        raise RuntimeError(
            "proposal risk execution evidence row validation descriptors are unavailable"
        )
    if any(value is None for _, value in result_descriptor_witnesses):
        raise RuntimeError(
            "proposal risk execution evidence result truth descriptors are unavailable"
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

    def sealed_mint(values, *, mint_capability):
        if module._mint is not sealed_mint:
            raise error_type(
                "proposal risk execution evidence sealed minter was rebound"
            )
        if mint_capability is not canonical_mint_capability:
            raise error_type(
                "proposal risk execution evidence mint capability is invalid"
            )
        if canonical_mint.__code__ is not canonical_mint_code:
            raise error_type(
                "proposal risk execution evidence canonical minter changed"
            )
        return canonical_mint(values, mint_capability=mint_capability)

    sealed_mint_code = sealed_mint.__code__
    module._mint = sealed_mint

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
        if module._mint is not sealed_mint or sealed_mint.__code__ is not sealed_mint_code:
            raise error_type(
                "proposal risk execution evidence helper _mint changed"
            )
        if canonical_mint.__code__ is not canonical_mint_code:
            raise error_type(
                "proposal risk execution evidence canonical minter changed"
            )
        current_kwdefaults = canonical_derive.__kwdefaults__
        if (
            type(current_kwdefaults) is not dict
            or set(current_kwdefaults) != {"_mint_capability"}
            or current_kwdefaults["_mint_capability"] is not canonical_mint_capability
        ):
            raise error_type(
                "proposal risk execution evidence derivation mint capability changed"
            )
        for name, expected in module_root_witnesses:
            if getattr(module, name, None) is not expected:
                raise error_type(
                    f"proposal risk execution evidence protocol root {name} changed"
                )
        for name, expected in precommit_descriptor_witnesses:
            if precommit_type.__dict__.get(name) is not expected:
                raise error_type(
                    f"proposal risk execution evidence precommit descriptor {name} changed"
                )
        for name, expected in row_descriptor_witnesses:
            if row_type.__dict__.get(name) is not expected:
                raise error_type(
                    f"proposal risk execution evidence row descriptor {name} changed"
                )
        for name, expected in result_descriptor_witnesses:
            if result_type.__dict__.get(name) is not expected:
                raise error_type(
                    f"proposal risk execution evidence result descriptor {name} changed"
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
        if type(rows) is tuple:
            for index, row in enumerate(rows):
                if type(row) is not row_type:
                    raise error_type(
                        f"rows[{index}] must be exact CounterfactualMemberExecutionEvidence"
                    )
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
_seal_write_once_slots(
    _execution_authority.ProductProposalRiskEvaluationPrecommit,
    _authority._BINDING_FIELDS,
)
_seal_write_once_slots(
    _execution_authority.CounterfactualMemberExecutionEvidence,
    (
        "member_id",
        "binding_sha256",
        "target_sha256",
        "candidate_vector_sha256",
        "executed_stakes",
        "execution_engine_sha256",
        "source_sha256",
        "observed_at",
        "starting_equity",
        "minimum_equity",
        "terminal_equity",
        "gross_pnl",
        "costs",
        "net_pnl",
    ),
)
_seal_write_once_slots(
    _execution_authority.ProductProposalRiskExecutionEvidence,
    _execution_authority._RESULT_FIELDS,
)
_install_execution_evidence_guard()
del _seal_write_once_slots
del _WriteOnceSlot
del _install_precommit_guard
del _install_execution_evidence_guard
del _authority
del _execution_authority
