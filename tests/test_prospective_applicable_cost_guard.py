from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.campaign_cost_evidence import CostClass
import autosport.prospective_applicable_cost as cost
import autosport.prospective_applicable_cost_guard as guard
from prospective_applicable_cost_test_support import canonical_applicable_cost_case


def _copy_component(component):
    copy = object.__new__(cost._SEALED_CANONICAL_TYPES[1])
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
    copy = object.__new__(cost._SEALED_CANONICAL_TYPES[2])
    object.__setattr__(copy, "intent_sha256", result.intent_sha256)
    object.__setattr__(copy, "opportunity_id", result.opportunity_id)
    object.__setattr__(copy, "portfolio_plan_sha256", result.portfolio_plan_sha256)
    object.__setattr__(copy, "decision_at", result.decision_at)
    object.__setattr__(copy, "components", result.components if components is None else components)
    object.__setattr__(copy, "completeness", result.completeness)
    object.__setattr__(copy, "total_subtractable_amount", None)
    object.__setattr__(copy, "currency", None)
    return copy


def _canonical(case):
    intent, plan, store, request, decision_at = case
    result = cost.resolve_prospective_applicable_costs(
        intent=intent,
        plan=plan,
        router_store=store,
        model_request_id=request.request_id,
        decision_at=decision_at,
    )
    return result


def _require(asserted, case):
    intent, plan, store, request, decision_at = case
    return guard.require_canonical_prospective_applicable_costs(
        asserted,
        intent=intent,
        plan=plan,
        router_store=store,
        model_request_id=request.request_id,
        decision_at=decision_at,
    )


def test_byte_equivalent_object_new_assertion_is_never_returned_as_authority():
    with canonical_applicable_cost_case() as case:
        canonical = _canonical(case)
        asserted = _copy_resolution(
            canonical,
            components=tuple(_copy_component(item) for item in canonical.components),
        )
        cost._SEALED_RESOLUTION_VALIDATOR(asserted)

        accepted = _require(asserted, case)

    assert accepted is not asserted
    assert type(accepted) is cost._SEALED_CANONICAL_TYPES[2]
    assert accepted.evidence_id == asserted.evidence_id


def test_valid_object_new_source_forge_is_rejected_after_fresh_reresolution():
    with canonical_applicable_cost_case() as case:
        canonical = _canonical(case)
        components = list(canonical.components)
        index = next(
            i for i, item in enumerate(components)
            if item.cost_class is CostClass.MODEL_COMPUTE_AI
        )
        forged = _copy_component(components[index])
        object.__setattr__(forged, "source_evidence_id", "f" * 64)
        object.__setattr__(forged, "source_sha256", "f" * 64)
        components[index] = forged
        asserted = _copy_resolution(canonical, components=tuple(components))
        cost._SEALED_RESOLUTION_VALIDATOR(asserted)

        with pytest.raises(
            cost.ProspectiveApplicableCostError,
            match="source_evidence_id|source_sha256",
        ):
            _require(asserted, case)


def test_valid_object_new_plan_identity_forge_is_rejected_after_reresolution():
    with canonical_applicable_cost_case() as case:
        canonical = _canonical(case)
        asserted = _copy_resolution(canonical)
        object.__setattr__(asserted, "portfolio_plan_sha256", "e" * 64)
        cost._SEALED_RESOLUTION_VALIDATOR(asserted)

        with pytest.raises(
            cost.ProspectiveApplicableCostError,
            match="portfolio_plan_sha256",
        ):
            _require(asserted, case)


def test_exact_class_positive_object_new_forge_fails_before_rebound_resolver_executes(
    monkeypatch,
):
    attacker_called = False

    def attacker_resolver(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound guard resolver must never execute")

    with canonical_applicable_cost_case() as case:
        canonical = _canonical(case)
        asserted = _copy_resolution(canonical)
        object.__setattr__(asserted, "completeness", "COMPLETE")
        object.__setattr__(asserted, "total_subtractable_amount", Decimal("7"))
        object.__setattr__(asserted, "currency", "EUR")

        monkeypatch.setattr(guard, "resolve_prospective_applicable_costs", attacker_resolver)
        with pytest.raises(
            cost.ProspectiveApplicableCostError,
            match="cannot represent COMPLETE",
        ):
            _require(asserted, case)

    assert attacker_called is False


def test_guard_resolver_type_and_field_globals_are_non_authoritative(monkeypatch):
    attacker_called = False

    def attacker_resolver(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("guard module mirror must never execute")

    with canonical_applicable_cost_case() as case:
        canonical = _canonical(case)
        asserted = _copy_resolution(
            canonical,
            components=tuple(_copy_component(item) for item in canonical.components),
        )

        monkeypatch.setattr(guard, "resolve_prospective_applicable_costs", attacker_resolver)
        monkeypatch.setattr(guard, "ProspectiveApplicableCostResolution", object)
        monkeypatch.setattr(guard, "ProspectiveApplicableCostComponent", object)
        monkeypatch.setattr(guard, "OpportunityIntent", object)
        monkeypatch.setattr(guard, "PortfolioPlan", object)
        monkeypatch.setattr(guard, "ModelComputeRouterStore", object)
        monkeypatch.setattr(guard, "_COMPONENT_FIELDS", ("attacker",))
        monkeypatch.setattr(guard, "_RESOLUTION_FIELDS", ("attacker",))

        accepted = _require(asserted, case)

    assert attacker_called is False
    assert accepted is not asserted
    cost._SEALED_RESOLUTION_VALIDATOR(accepted)


def test_source_resolver_type_validator_and_semantic_globals_are_non_authoritative_to_guard(
    monkeypatch,
):
    attacker_called = False

    def attacker_source_resolver(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound source resolver must never execute")

    with canonical_applicable_cost_case() as case:
        canonical = _canonical(case)
        asserted = _copy_resolution(
            canonical,
            components=tuple(_copy_component(item) for item in canonical.components),
        )

        monkeypatch.setattr(cost, "resolve_prospective_applicable_costs", attacker_source_resolver)
        monkeypatch.setattr(cost, "OpportunityIntent", object)
        monkeypatch.setattr(cost, "PortfolioPlan", object)
        monkeypatch.setattr(cost, "ModelComputeRouterStore", object)
        monkeypatch.setattr(cost, "ProspectiveApplicableCostResolution", object)
        monkeypatch.setattr(cost, "ProspectiveApplicableCostComponent", object)
        monkeypatch.setattr(
            cost,
            "_SEALED_RESOLUTION_VALIDATOR",
            lambda _value: (_ for _ in ()).throw(
                AssertionError("rebound validator must never execute")
            ),
        )
        cost._STATUS_AXES.clear()
        cost._COMPONENT_SEMANTICS.clear()
        monkeypatch.setattr(cost, "REQUIRED_COST_CLASSES", frozenset())

        accepted = _require(asserted, case)

    assert attacker_called is False
    assert len(accepted.components) == 5


def test_noncanonical_resolution_subclass_rejected_before_rebound_resolver_executes(
    monkeypatch,
):
    resolution_cls = cost._SEALED_CANONICAL_TYPES[2]

    class ResolutionSubclass(resolution_cls):
        pass

    asserted = object.__new__(ResolutionSubclass)
    attacker_called = False

    def attacker_resolver(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("attacker resolver must never execute")

    monkeypatch.setattr(guard, "resolve_prospective_applicable_costs", attacker_resolver)
    with canonical_applicable_cost_case() as case:
        with pytest.raises(
            cost.ProspectiveApplicableCostError,
            match="exact canonical type",
        ):
            _require(asserted, case)

    assert attacker_called is False
