from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.campaign_cost_evidence import CostClass, REQUIRED_COST_CLASSES
import autosport.prospective_applicable_cost as cost
import autosport.prospective_applicable_cost_guard as guard


DECISION_AT = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)


def _component(
    cost_class: CostClass,
    *,
    model_source_id: str = "b" * 64,
) -> cost.ProspectiveApplicableCostComponent:
    semantics = {
        CostClass.MODEL_COMPUTE_AI: (
            cost.ProspectiveCostResolutionStatus.UNKNOWN_UNPROVEN,
            cost.ProspectiveApplicableCostReason.MODEL_COMPUTE_AUTHORITY_UNRESOLVED,
            (),
            "autosport.prospective_model_compute_money",
            model_source_id,
            model_source_id,
        ),
        CostClass.PROVIDER_DATA: (
            cost.ProspectiveCostResolutionStatus.UNKNOWN_UNPROVEN,
            cost.ProspectiveApplicableCostReason.NO_PROSPECTIVE_PROVIDER_DATA_AUTHORITY,
            (),
            None,
            None,
            None,
        ),
        CostClass.FIXED_CAMPAIGN: (
            cost.ProspectiveCostResolutionStatus.UNKNOWN_UNPROVEN,
            cost.ProspectiveApplicableCostReason.NO_PROSPECTIVE_FIXED_ALLOCATION_AUTHORITY,
            (),
            None,
            None,
            None,
        ),
        CostClass.EXECUTION_SLIPPAGE: (
            cost.ProspectiveCostResolutionStatus.EXECUTION_STATE_DEPENDENT,
            cost.ProspectiveApplicableCostReason.EXECUTION_SLIPPAGE_DEPENDS_ON_EXECUTION,
            (cost.ProspectiveCostDependencyAxis.EXECUTION_STATE,),
            None,
            None,
            None,
        ),
        CostClass.EXECUTION_FEES_COMMISSION_TAX: (
            cost.ProspectiveCostResolutionStatus.EXECUTION_AND_TERMINAL_STATE_DEPENDENT,
            cost.ProspectiveApplicableCostReason.EXECUTION_FEES_DEPEND_ON_EXECUTION_OR_TERMINAL_STATE,
            (
                cost.ProspectiveCostDependencyAxis.EXECUTION_STATE,
                cost.ProspectiveCostDependencyAxis.TERMINAL_STATE,
            ),
            None,
            None,
            None,
        ),
    }
    status, reason, axes, family, evidence_id, source_sha256 = semantics[cost_class]
    component = object.__new__(cost.ProspectiveApplicableCostComponent)
    object.__setattr__(component, "cost_class", cost_class)
    object.__setattr__(component, "status", status)
    object.__setattr__(component, "reason", reason)
    object.__setattr__(component, "dependency_axes", axes)
    object.__setattr__(component, "source_family", family)
    object.__setattr__(component, "source_evidence_id", evidence_id)
    object.__setattr__(component, "source_sha256", source_sha256)
    component.__post_init__()
    return component


def _resolution(
    *,
    model_source_id: str = "b" * 64,
    intent_sha256: str = "a" * 64,
    opportunity_id: str = "opportunity-729",
    plan_sha256: str = "d" * 64,
) -> cost.ProspectiveApplicableCostResolution:
    components = tuple(
        _component(cost_class, model_source_id=model_source_id)
        for cost_class in sorted(REQUIRED_COST_CLASSES, key=lambda value: value.value)
    )
    resolution = object.__new__(cost.ProspectiveApplicableCostResolution)
    object.__setattr__(resolution, "intent_sha256", intent_sha256)
    object.__setattr__(resolution, "opportunity_id", opportunity_id)
    object.__setattr__(resolution, "portfolio_plan_sha256", plan_sha256)
    object.__setattr__(resolution, "decision_at", DECISION_AT)
    object.__setattr__(resolution, "components", components)
    object.__setattr__(
        resolution,
        "completeness",
        cost.ProspectiveApplicableCostCompleteness.INCOMPLETE,
    )
    object.__setattr__(resolution, "total_subtractable_amount", None)
    object.__setattr__(resolution, "currency", None)
    resolution.__post_init__()
    return resolution


def _accept(monkeypatch: pytest.MonkeyPatch, asserted, canonical):
    seen: dict[str, object] = {}

    def canonical_resolver(**kwargs):
        seen.update(kwargs)
        return canonical

    monkeypatch.setattr(guard, "resolve_prospective_applicable_costs", canonical_resolver)
    accepted = guard.require_canonical_prospective_applicable_costs(
        asserted,
        intent=object(),  # resolver owns runtime type validation in production
        plan=object(),
        router_store=object(),
        model_request_id="request-729",
        decision_at=DECISION_AT,
    )
    return accepted, seen


def test_byte_equivalent_object_new_assertion_is_not_returned_as_authority(monkeypatch):
    canonical = _resolution()
    asserted = _resolution()
    assert asserted is not canonical
    assert asserted.evidence_id == canonical.evidence_id

    accepted, seen = _accept(monkeypatch, asserted, canonical)

    assert accepted is canonical
    assert accepted is not asserted
    assert seen["model_request_id"] == "request-729"
    assert seen["decision_at"] == DECISION_AT


def test_valid_tuple_object_new_source_forge_is_rejected_after_reresolution(monkeypatch):
    canonical = _resolution(model_source_id="b" * 64)
    asserted = _resolution(model_source_id="c" * 64)

    # Both values are locally shape-valid and have normal evidence ids.  Only
    # canonical re-resolution distinguishes the caller assertion from truth.
    assert asserted.evidence_id != canonical.evidence_id
    with pytest.raises(
        cost.ProspectiveApplicableCostError,
        match="source_evidence_id|source_sha256",
    ):
        _accept(monkeypatch, asserted, canonical)


def test_valid_tuple_object_new_plan_identity_forge_is_rejected(monkeypatch):
    canonical = _resolution(plan_sha256="d" * 64)
    asserted = _resolution(plan_sha256="e" * 64)

    with pytest.raises(
        cost.ProspectiveApplicableCostError,
        match="portfolio_plan_sha256",
    ):
        _accept(monkeypatch, asserted, canonical)


def test_valid_tuple_object_new_intent_identity_forge_is_rejected(monkeypatch):
    canonical = _resolution(intent_sha256="a" * 64)
    asserted = _resolution(intent_sha256="f" * 64)

    with pytest.raises(
        cost.ProspectiveApplicableCostError,
        match="intent_sha256",
    ):
        _accept(monkeypatch, asserted, canonical)


def test_component_count_forge_is_rejected(monkeypatch):
    canonical = _resolution()
    asserted = _resolution()
    object.__setattr__(asserted, "components", asserted.components[:-1])

    with pytest.raises(
        cost.ProspectiveApplicableCostError,
        match="component count",
    ):
        _accept(monkeypatch, asserted, canonical)


def test_noncanonical_resolution_type_is_rejected_before_authority_read(monkeypatch):
    called = False

    class ResolutionSubclass(cost.ProspectiveApplicableCostResolution):
        pass

    asserted = object.__new__(ResolutionSubclass)

    def canonical_resolver(**_kwargs):
        nonlocal called
        called = True
        return _resolution()

    monkeypatch.setattr(guard, "resolve_prospective_applicable_costs", canonical_resolver)
    with pytest.raises(
        cost.ProspectiveApplicableCostError,
        match="exact canonical type",
    ):
        guard.require_canonical_prospective_applicable_costs(
            asserted,
            intent=object(),
            plan=object(),
            router_store=object(),
            model_request_id="request-729",
            decision_at=DECISION_AT,
        )
    assert called is False


def test_noncanonical_resolver_result_fails_closed(monkeypatch):
    asserted = _resolution()
    monkeypatch.setattr(
        guard,
        "resolve_prospective_applicable_costs",
        lambda **_kwargs: object(),
    )

    with pytest.raises(
        cost.ProspectiveApplicableCostError,
        match="resolver returned a non-canonical resolution type",
    ):
        guard.require_canonical_prospective_applicable_costs(
            asserted,
            intent=object(),
            plan=object(),
            router_store=object(),
            model_request_id="request-729",
            decision_at=DECISION_AT,
        )
