from __future__ import annotations

from decimal import Decimal
import inspect

import pytest

from autosport.campaign_cost_evidence import CostClass, REQUIRED_COST_CLASSES
import autosport.prospective_applicable_cost as subject
from prospective_applicable_cost_test_support import canonical_applicable_cost_case


def _resolve_real():
    with canonical_applicable_cost_case() as (intent, plan, store, request, decision_at):
        return subject.resolve_prospective_applicable_costs(
            intent=intent,
            plan=plan,
            router_store=store,
            model_request_id=request.request_id,
            decision_at=decision_at,
        )


def _copy_component(component):
    copy = object.__new__(subject.ProspectiveApplicableCostComponent)
    for field in (
        "cost_class",
        "status",
        "reason",
        "dependency_axes",
        "source_family",
        "source_evidence_id",
        "source_sha256",
    ):
        object.__setattr__(copy, field, object.__getattribute__(component, field))
    return copy


def _copy_resolution(result, *, components=None):
    copy = object.__new__(subject.ProspectiveApplicableCostResolution)
    object.__setattr__(copy, "intent_sha256", result.intent_sha256)
    object.__setattr__(copy, "opportunity_id", result.opportunity_id)
    object.__setattr__(copy, "portfolio_plan_sha256", result.portfolio_plan_sha256)
    object.__setattr__(copy, "decision_at", result.decision_at)
    object.__setattr__(copy, "components", result.components if components is None else components)
    object.__setattr__(copy, "completeness", result.completeness)
    object.__setattr__(copy, "total_subtractable_amount", None)
    object.__setattr__(copy, "currency", None)
    return copy


def test_real_types_compose_through_sealed_router_and_model_money_authority():
    result = _resolve_real()

    assert type(result) is subject.ProspectiveApplicableCostResolution
    assert result.completeness is subject.ProspectiveApplicableCostCompleteness.INCOMPLETE
    assert result.total_subtractable_amount is None
    assert result.currency is None
    assert tuple(item.cost_class for item in result.components) == tuple(
        sorted(REQUIRED_COST_CLASSES, key=lambda value: value.value)
    )
    by_class = {item.cost_class: item for item in result.components}
    assert by_class[CostClass.EXECUTION_SLIPPAGE].dependency_axes == (
        subject.ProspectiveCostDependencyAxis.EXECUTION_STATE,
    )
    assert by_class[CostClass.EXECUTION_FEES_COMMISSION_TAX].dependency_axes == (
        subject.ProspectiveCostDependencyAxis.EXECUTION_STATE,
        subject.ProspectiveCostDependencyAxis.TERMINAL_STATE,
    )
    assert by_class[CostClass.MODEL_COMPUTE_AI].source_evidence_id is not None


def test_public_resolver_has_no_caller_money_applicability_or_source_inputs():
    parameters = set(inspect.signature(subject.resolve_prospective_applicable_costs).parameters)
    assert parameters == {"intent", "plan", "router_store", "model_request_id", "decision_at"}
    assert not parameters.intersection(
        {"amount", "currency", "costs", "applicability", "source", "source_ref", "known_zero"}
    )


def test_public_constructors_cannot_mint_positive_or_component_authority():
    with pytest.raises(subject.ProspectiveApplicableCostError, match="resolver-owned"):
        subject.ProspectiveApplicableCostComponent()
    with pytest.raises(subject.ProspectiveApplicableCostError, match="resolver-owned"):
        subject.ProspectiveApplicableCostResolution(
            completeness="COMPLETE",
            total_subtractable_amount=Decimal("1"),
            currency="EUR",
        )


def test_sealed_validator_rejects_exact_object_new_positive_resolution():
    result = _resolve_real()
    forged = _copy_resolution(result)
    object.__setattr__(forged, "completeness", "COMPLETE")
    object.__setattr__(forged, "total_subtractable_amount", Decimal("1"))
    object.__setattr__(forged, "currency", "EUR")

    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="cannot represent COMPLETE",
    ):
        subject._SEALED_RESOLUTION_VALIDATOR(forged)


def test_sealed_validator_rejects_semantic_table_reauthoring():
    result = _resolve_real()
    components = list(result.components)
    index = next(
        i for i, item in enumerate(components)
        if item.cost_class is CostClass.EXECUTION_SLIPPAGE
    )
    forged = _copy_component(components[index])
    object.__setattr__(
        forged,
        "status",
        subject.ProspectiveCostResolutionStatus.TERMINAL_STATE_DEPENDENT,
    )
    object.__setattr__(
        forged,
        "dependency_axes",
        (subject.ProspectiveCostDependencyAxis.TERMINAL_STATE,),
    )
    components[index] = forged
    resolution = _copy_resolution(result, components=tuple(components))

    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="sealed canonical schema-v2 semantic tuple",
    ):
        subject._SEALED_RESOLUTION_VALIDATOR(resolution)


def test_sealed_validator_rejects_duplicate_or_missing_required_class():
    result = _resolve_real()
    components = result.components[:-1] + (result.components[0],)
    forged = _copy_resolution(result, components=components)

    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="each required cost class exactly once",
    ):
        subject._SEALED_RESOLUTION_VALIDATOR(forged)


def test_module_type_model_resolver_and_semantic_rebinding_cannot_redirect_sealed_resolver(
    monkeypatch,
):
    sealed_resolver = subject.resolve_prospective_applicable_costs
    attacker_called = False

    def attacker_model_resolver(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("forged model-money resolver must never execute")

    monkeypatch.setattr(subject, "OpportunityIntent", object)
    monkeypatch.setattr(subject, "PortfolioPlan", object)
    monkeypatch.setattr(subject, "ModelComputeRouterStore", object)
    monkeypatch.setattr(subject, "ProspectiveModelComputeMoneyEvidence", object)
    monkeypatch.setattr(subject, "resolve_prospective_model_compute_money", attacker_model_resolver)
    monkeypatch.setattr(subject, "ProspectiveApplicableCostComponent", object)
    monkeypatch.setattr(subject, "ProspectiveApplicableCostResolution", object)
    subject._STATUS_AXES.clear()
    subject._COMPONENT_SEMANTICS.clear()
    monkeypatch.setattr(subject, "REQUIRED_COST_CLASSES", frozenset())

    with canonical_applicable_cost_case() as (intent, plan, store, request, decision_at):
        result = sealed_resolver(
            intent=intent,
            plan=plan,
            router_store=store,
            model_request_id=request.request_id,
            decision_at=decision_at,
        )

    assert attacker_called is False
    sealed_types = subject._SEALED_CANONICAL_TYPES
    assert type(result) is sealed_types[2]
    subject._SEALED_RESOLUTION_VALIDATOR(result)
    assert len(result.components) == 5


def test_public_resolver_name_rebinding_does_not_mutate_preinstalled_sealed_capability(
    monkeypatch,
):
    sealed_resolver = subject.resolve_prospective_applicable_costs
    attacker_called = False

    def attacker_resolver(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound public mirror is not sealed authority")

    monkeypatch.setattr(subject, "resolve_prospective_applicable_costs", attacker_resolver)

    with canonical_applicable_cost_case() as (intent, plan, store, request, decision_at):
        result = sealed_resolver(
            intent=intent,
            plan=plan,
            router_store=store,
            model_request_id=request.request_id,
            decision_at=decision_at,
        )

    assert attacker_called is False
    subject._SEALED_RESOLUTION_VALIDATOR(result)


def test_schema_v2_dependency_axes_cannot_be_flattened_to_scalar_money():
    result = _resolve_real()
    by_class = {item.cost_class: item for item in result.components}
    slippage = by_class[CostClass.EXECUTION_SLIPPAGE]
    fees = by_class[CostClass.EXECUTION_FEES_COMMISSION_TAX]

    assert slippage.status is subject.ProspectiveCostResolutionStatus.EXECUTION_STATE_DEPENDENT
    assert subject.ProspectiveCostDependencyAxis.TERMINAL_STATE not in slippage.dependency_axes
    assert fees.status is (
        subject.ProspectiveCostResolutionStatus.EXECUTION_AND_TERMINAL_STATE_DEPENDENT
    )
    assert subject.ProspectiveCostDependencyAxis.TERMINAL_STATE in fees.dependency_axes
    assert all(item.to_dict()["amount"] is None for item in result.components)
    assert all(item.to_dict()["currency"] is None for item in result.components)


def test_exact_input_subclasses_are_rejected_before_product_authority_read():
    sealed_types = subject._SEALED_CANONICAL_TYPES
    intent_cls, plan_cls, store_cls = sealed_types[3:]

    class IntentSubclass(intent_cls):
        pass

    class PlanSubclass(plan_cls):
        pass

    class StoreSubclass(store_cls):
        pass

    with canonical_applicable_cost_case() as (intent, plan, store, request, decision_at):
        cases = (
            (IntentSubclass.__new__(IntentSubclass), plan, store, "OpportunityIntent"),
            (intent, PlanSubclass.__new__(PlanSubclass), store, "PortfolioPlan"),
            (intent, plan, StoreSubclass.__new__(StoreSubclass), "ModelComputeRouterStore"),
        )
        for candidate_intent, candidate_plan, candidate_store, error in cases:
            with pytest.raises(subject.ProspectiveApplicableCostError, match=error):
                subject.resolve_prospective_applicable_costs(
                    intent=candidate_intent,
                    plan=candidate_plan,
                    router_store=candidate_store,
                    model_request_id=request.request_id,
                    decision_at=decision_at,
                )
