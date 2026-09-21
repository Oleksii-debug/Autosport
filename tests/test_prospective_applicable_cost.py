from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import inspect

import pytest

from autosport.campaign_cost_evidence import CostClass, REQUIRED_COST_CLASSES
from autosport.prospective_model_compute_money import ProspectiveModelComputeMoneyStatus

import autosport.prospective_applicable_cost as subject


DECISION_AT = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)


class _Opportunity:
    opportunity_id = "opportunity-729"


class _RiskContext:
    proposal_ts = "2026-09-21T00:00:00Z"


class _Intent:
    intent_sha256 = "a" * 64
    opportunity = _Opportunity()
    risk_context = _RiskContext()


class _Store:
    pass


class _ModelEvidence:
    def __init__(
        self,
        *,
        evidence_id: str = "b" * 64,
        intent_sha256: str = "a" * 64,
        opportunity_id: str = "opportunity-729",
        decision_at: datetime = DECISION_AT,
    ) -> None:
        self.evidence_id = evidence_id
        self.intent_sha256 = intent_sha256
        self.opportunity_id = opportunity_id
        self.decision_at = decision_at
        self.status = ProspectiveModelComputeMoneyStatus.UNKNOWN_UNPROVEN


def _patch_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "OpportunityIntent", _Intent)
    monkeypatch.setattr(subject, "ModelComputeRouterStore", _Store)
    monkeypatch.setattr(subject, "ProspectiveModelComputeMoneyEvidence", _ModelEvidence)


def _resolve(monkeypatch: pytest.MonkeyPatch, evidence: _ModelEvidence | None = None):
    _patch_types(monkeypatch)
    seen: dict[str, object] = {}

    def canonical_model_resolver(**kwargs):
        seen.update(kwargs)
        return evidence or _ModelEvidence()

    monkeypatch.setattr(
        subject,
        "resolve_prospective_model_compute_money",
        canonical_model_resolver,
    )
    result = subject.resolve_prospective_applicable_costs(
        intent=_Intent(),
        router_store=_Store(),
        model_request_id="request-729",
        decision_at=DECISION_AT,
    )
    return result, seen


def test_current_product_truth_is_explicitly_incomplete_without_zero_invention(monkeypatch):
    result, _ = _resolve(monkeypatch)

    assert (
        result.completeness
        is subject.ProspectiveApplicableCostCompleteness.INCOMPLETE
    )
    assert result.total_subtractable_amount is None
    assert result.currency is None
    assert tuple(item.cost_class for item in result.components) == tuple(
        sorted(REQUIRED_COST_CLASSES, key=lambda value: value.value)
    )
    assert all(item.to_dict()["amount"] is None for item in result.components)
    assert all(item.to_dict()["currency"] is None for item in result.components)
    assert result.to_dict()["evidence_id"] == result.evidence_id


def test_model_compute_is_re_resolved_through_canonical_product_authority(monkeypatch):
    result, seen = _resolve(monkeypatch)

    assert type(seen["intent"]) is _Intent
    assert type(seen["router_store"]) is _Store
    assert seen["request_id"] == "request-729"
    assert seen["decision_at"] == DECISION_AT
    model = next(
        item for item in result.components
        if item.cost_class is CostClass.MODEL_COMPUTE_AI
    )
    assert model.status is subject.ProspectiveCostResolutionStatus.UNKNOWN_UNPROVEN
    assert model.source_family == "autosport.prospective_model_compute_money"
    assert model.source_evidence_id == "b" * 64
    assert model.source_sha256 == "b" * 64


def test_public_resolver_has_no_caller_money_applicability_or_source_inputs():
    parameters = set(inspect.signature(subject.resolve_prospective_applicable_costs).parameters)
    assert parameters == {
        "intent",
        "router_store",
        "model_request_id",
        "decision_at",
    }
    assert not parameters.intersection(
        {
            "amount",
            "currency",
            "costs",
            "applicability",
            "source",
            "source_ref",
            "known_zero",
        }
    )


def test_schema_v1_cannot_be_minted_complete_or_with_positive_total(monkeypatch):
    result, _ = _resolve(monkeypatch)

    with pytest.raises(subject.ProspectiveApplicableCostError, match="cannot represent COMPLETE"):
        subject.ProspectiveApplicableCostResolution(
            intent_sha256=result.intent_sha256,
            opportunity_id=result.opportunity_id,
            decision_at=result.decision_at,
            components=result.components,
            completeness="COMPLETE",  # type: ignore[arg-type]
        )

    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="cannot represent an authoritative monetary total",
    ):
        subject.ProspectiveApplicableCostResolution(
            intent_sha256=result.intent_sha256,
            opportunity_id=result.opportunity_id,
            decision_at=result.decision_at,
            components=result.components,
            total_subtractable_amount=Decimal("0"),  # type: ignore[arg-type]
        )


def test_terminal_state_dependent_execution_costs_remain_nonzero_unknown(monkeypatch):
    result, _ = _resolve(monkeypatch)
    by_class = {item.cost_class: item for item in result.components}

    assert (
        by_class[CostClass.EXECUTION_SLIPPAGE].status
        is subject.ProspectiveCostResolutionStatus.TERMINAL_STATE_DEPENDENT
    )
    assert (
        by_class[CostClass.EXECUTION_FEES_COMMISSION_TAX].status
        is subject.ProspectiveCostResolutionStatus.TERMINAL_STATE_DEPENDENT
    )
    for cost_class in (
        CostClass.EXECUTION_SLIPPAGE,
        CostClass.EXECUTION_FEES_COMMISSION_TAX,
    ):
        assert by_class[cost_class].to_dict()["amount"] is None


def test_unsupported_provider_and_fixed_classes_stay_explicitly_unresolved(monkeypatch):
    result, _ = _resolve(monkeypatch)
    by_class = {item.cost_class: item for item in result.components}

    assert by_class[CostClass.PROVIDER_DATA].reason is (
        subject.ProspectiveApplicableCostReason.NO_PROSPECTIVE_PROVIDER_DATA_AUTHORITY
    )
    assert by_class[CostClass.FIXED_CAMPAIGN].reason is (
        subject.ProspectiveApplicableCostReason.NO_PROSPECTIVE_FIXED_ALLOCATION_AUTHORITY
    )
    assert by_class[CostClass.PROVIDER_DATA].source_family is None
    assert by_class[CostClass.FIXED_CAMPAIGN].source_family is None


def test_cross_intent_model_evidence_is_rejected(monkeypatch):
    _patch_types(monkeypatch)
    monkeypatch.setattr(
        subject,
        "resolve_prospective_model_compute_money",
        lambda **_kwargs: _ModelEvidence(intent_sha256="c" * 64),
    )

    with pytest.raises(subject.ProspectiveApplicableCostError, match="intent mismatch"):
        subject.resolve_prospective_applicable_costs(
            intent=_Intent(),
            router_store=_Store(),
            model_request_id="request-729",
            decision_at=DECISION_AT,
        )


def test_cross_opportunity_model_evidence_is_rejected(monkeypatch):
    _patch_types(monkeypatch)
    monkeypatch.setattr(
        subject,
        "resolve_prospective_model_compute_money",
        lambda **_kwargs: _ModelEvidence(opportunity_id="different-opportunity"),
    )

    with pytest.raises(subject.ProspectiveApplicableCostError, match="opportunity mismatch"):
        subject.resolve_prospective_applicable_costs(
            intent=_Intent(),
            router_store=_Store(),
            model_request_id="request-729",
            decision_at=DECISION_AT,
        )


def test_caller_cannot_move_decision_cutoff(monkeypatch):
    _patch_types(monkeypatch)
    called = False

    def resolver(**_kwargs):
        nonlocal called
        called = True
        return _ModelEvidence()

    monkeypatch.setattr(subject, "resolve_prospective_model_compute_money", resolver)

    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="caller decision_at does not match",
    ):
        subject.resolve_prospective_applicable_costs(
            intent=_Intent(),
            router_store=_Store(),
            model_request_id="request-729",
            decision_at=datetime(2026, 9, 21, 0, 1, tzinfo=timezone.utc),
        )

    assert called is False


def test_subclass_substitution_is_rejected_before_authority_read(monkeypatch):
    _patch_types(monkeypatch)
    called = False

    def resolver(**_kwargs):
        nonlocal called
        called = True
        return _ModelEvidence()

    monkeypatch.setattr(subject, "resolve_prospective_model_compute_money", resolver)

    class IntentSubclass(_Intent):
        pass

    class StoreSubclass(_Store):
        pass

    with pytest.raises(subject.ProspectiveApplicableCostError, match="exact canonical OpportunityIntent"):
        subject.resolve_prospective_applicable_costs(
            intent=IntentSubclass(),
            router_store=_Store(),
            model_request_id="request-729",
            decision_at=DECISION_AT,
        )
    with pytest.raises(subject.ProspectiveApplicableCostError, match="exact canonical ModelComputeRouterStore"):
        subject.resolve_prospective_applicable_costs(
            intent=_Intent(),
            router_store=StoreSubclass(),
            model_request_id="request-729",
            decision_at=DECISION_AT,
        )

    assert called is False


def test_duplicate_or_missing_required_cost_class_is_rejected(monkeypatch):
    result, _ = _resolve(monkeypatch)

    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="each required cost class exactly once",
    ):
        subject.ProspectiveApplicableCostResolution(
            intent_sha256=result.intent_sha256,
            opportunity_id=result.opportunity_id,
            decision_at=result.decision_at,
            components=result.components[:-1] + (result.components[0],),
        )


def test_model_source_substitution_changes_aggregate_evidence_identity(monkeypatch):
    first, _ = _resolve(monkeypatch, _ModelEvidence(evidence_id="b" * 64))
    second, _ = _resolve(monkeypatch, _ModelEvidence(evidence_id="c" * 64))

    assert first.evidence_id != second.evidence_id
