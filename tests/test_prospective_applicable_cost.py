from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import inspect
from pathlib import Path
import tempfile

import pytest

from autosport.campaign_cost_evidence import CostClass, REQUIRED_COST_CLASSES
from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    ModelComputeRouterStore,
)
from autosport.paper import PaperBook
from autosport.prospective_model_compute_money import ProspectiveModelComputeMoneyStatus

import autosport.prospective_applicable_cost as subject


DECISION_AT = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)


class _Opportunity:
    opportunity_id = "opportunity-729"


class _RiskContext:
    proposal_ts = "2026-09-21T00:00:00Z"


class _Intent:
    intent_id = "intent-729"
    intent_sha256 = "a" * 64
    opportunity = _Opportunity()
    risk_context = _RiskContext()


class _Plan:
    decision_ts = "2026-09-21T00:00:00Z"
    intent_ids = ("intent-729",)
    intent_sha256s = ("a" * 64,)
    plan_sha256 = "d" * 64


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
    monkeypatch.setattr(subject, "PortfolioPlan", _Plan)
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
    return (
        subject.resolve_prospective_applicable_costs(
            intent=_Intent(),
            plan=_Plan(),
            router_store=_Store(),
            model_request_id="request-729",
            decision_at=DECISION_AT,
        ),
        seen,
    )


def test_current_truth_is_incomplete_without_zero_invention(monkeypatch):
    result, _ = _resolve(monkeypatch)
    assert result.completeness is subject.ProspectiveApplicableCostCompleteness.INCOMPLETE
    assert result.total_subtractable_amount is None
    assert result.currency is None
    assert result.portfolio_plan_sha256 == "d" * 64
    assert tuple(item.cost_class for item in result.components) == tuple(
        sorted(REQUIRED_COST_CLASSES, key=lambda value: value.value)
    )
    assert all(item.to_dict()["amount"] is None for item in result.components)
    assert all(item.to_dict()["currency"] is None for item in result.components)
    assert result.to_dict()["evidence_id"] == result.evidence_id


def test_dependency_axes_preserve_execution_vs_terminal_dimensions(monkeypatch):
    result, _ = _resolve(monkeypatch)
    by_class = {item.cost_class: item for item in result.components}

    slippage = by_class[CostClass.EXECUTION_SLIPPAGE]
    assert slippage.status is subject.ProspectiveCostResolutionStatus.EXECUTION_STATE_DEPENDENT
    assert slippage.dependency_axes == (
        subject.ProspectiveCostDependencyAxis.EXECUTION_STATE,
    )

    fees = by_class[CostClass.EXECUTION_FEES_COMMISSION_TAX]
    assert fees.status is (
        subject.ProspectiveCostResolutionStatus.EXECUTION_AND_TERMINAL_STATE_DEPENDENT
    )
    assert fees.dependency_axes == (
        subject.ProspectiveCostDependencyAxis.EXECUTION_STATE,
        subject.ProspectiveCostDependencyAxis.TERMINAL_STATE,
    )
    assert fees.to_dict()["amount"] is None


def test_status_cannot_lie_about_dependency_axes():
    with pytest.raises(subject.ProspectiveApplicableCostError, match="status and dependency_axes"):
        subject.ProspectiveApplicableCostComponent(
            cost_class=CostClass.EXECUTION_SLIPPAGE,
            status=subject.ProspectiveCostResolutionStatus.TERMINAL_STATE_DEPENDENT,
            dependency_axes=(subject.ProspectiveCostDependencyAxis.EXECUTION_STATE,),
            reason=subject.ProspectiveApplicableCostReason.EXECUTION_SLIPPAGE_DEPENDS_ON_EXECUTION,
        )


def test_fee_dependency_cannot_be_flattened_to_execution_only_scalar(monkeypatch):
    result, _ = _resolve(monkeypatch)
    fee = next(
        item for item in result.components
        if item.cost_class is CostClass.EXECUTION_FEES_COMMISSION_TAX
    )
    assert subject.ProspectiveCostDependencyAxis.TERMINAL_STATE in fee.dependency_axes
    assert fee.status is not subject.ProspectiveCostResolutionStatus.EXECUTION_STATE_DEPENDENT
    assert fee.to_dict()["amount"] is None
    assert fee.to_dict()["currency"] is None


def test_model_compute_is_re_resolved_through_product_authority(monkeypatch):
    result, seen = _resolve(monkeypatch)
    assert type(seen["intent"]) is _Intent
    assert type(seen["router_store"]) is _Store
    assert seen["request_id"] == "request-729"
    assert seen["decision_at"] == DECISION_AT
    model = next(item for item in result.components if item.cost_class is CostClass.MODEL_COMPUTE_AI)
    assert model.status is subject.ProspectiveCostResolutionStatus.UNKNOWN_UNPROVEN
    assert model.dependency_axes == ()
    assert model.source_family == "autosport.prospective_model_compute_money"
    assert model.source_evidence_id == "b" * 64


def test_public_resolver_has_no_caller_money_applicability_or_source_inputs():
    parameters = set(inspect.signature(subject.resolve_prospective_applicable_costs).parameters)
    assert parameters == {"intent", "plan", "router_store", "model_request_id", "decision_at"}
    assert not parameters.intersection(
        {"amount", "currency", "costs", "applicability", "source", "source_ref", "known_zero"}
    )


def test_schema_v2_cannot_be_minted_complete_or_with_monetary_total(monkeypatch):
    result, _ = _resolve(monkeypatch)
    common = dict(
        intent_sha256=result.intent_sha256,
        opportunity_id=result.opportunity_id,
        portfolio_plan_sha256=result.portfolio_plan_sha256,
        decision_at=result.decision_at,
        components=result.components,
    )
    with pytest.raises(subject.ProspectiveApplicableCostError, match="cannot represent COMPLETE"):
        subject.ProspectiveApplicableCostResolution(
            **common,
            completeness="COMPLETE",  # type: ignore[arg-type]
        )
    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="cannot represent an authoritative monetary total",
    ):
        subject.ProspectiveApplicableCostResolution(
            **common,
            total_subtractable_amount=Decimal("0"),  # type: ignore[arg-type]
        )


def test_provider_and_fixed_classes_stay_unknown(monkeypatch):
    result, _ = _resolve(monkeypatch)
    by_class = {item.cost_class: item for item in result.components}
    assert by_class[CostClass.PROVIDER_DATA].reason is (
        subject.ProspectiveApplicableCostReason.NO_PROSPECTIVE_PROVIDER_DATA_AUTHORITY
    )
    assert by_class[CostClass.FIXED_CAMPAIGN].reason is (
        subject.ProspectiveApplicableCostReason.NO_PROSPECTIVE_FIXED_ALLOCATION_AUTHORITY
    )
    assert by_class[CostClass.PROVIDER_DATA].dependency_axes == ()
    assert by_class[CostClass.FIXED_CAMPAIGN].dependency_axes == ()


@pytest.mark.parametrize(
    ("evidence", "error"),
    [
        (_ModelEvidence(intent_sha256="c" * 64), "intent mismatch"),
        (_ModelEvidence(opportunity_id="different-opportunity"), "opportunity mismatch"),
        (
            _ModelEvidence(decision_at=datetime(2026, 9, 21, 0, 1, tzinfo=timezone.utc)),
            "decision cutoff mismatch",
        ),
    ],
)
def test_model_identity_substitution_is_rejected(monkeypatch, evidence, error):
    _patch_types(monkeypatch)
    monkeypatch.setattr(subject, "resolve_prospective_model_compute_money", lambda **_kwargs: evidence)
    with pytest.raises(subject.ProspectiveApplicableCostError, match=error):
        subject.resolve_prospective_applicable_costs(
            intent=_Intent(),
            plan=_Plan(),
            router_store=_Store(),
            model_request_id="request-729",
            decision_at=DECISION_AT,
        )


def test_caller_cannot_move_cutoff_before_authority_read(monkeypatch):
    _patch_types(monkeypatch)
    called = False

    def resolver(**_kwargs):
        nonlocal called
        called = True
        return _ModelEvidence()

    monkeypatch.setattr(subject, "resolve_prospective_model_compute_money", resolver)
    with pytest.raises(subject.ProspectiveApplicableCostError, match="caller decision_at does not match"):
        subject.resolve_prospective_applicable_costs(
            intent=_Intent(),
            plan=_Plan(),
            router_store=_Store(),
            model_request_id="request-729",
            decision_at=datetime(2026, 9, 21, 0, 1, tzinfo=timezone.utc),
        )
    assert called is False


def test_cross_plan_substitution_is_rejected_before_model_read(monkeypatch):
    _patch_types(monkeypatch)
    called = False

    def resolver(**_kwargs):
        nonlocal called
        called = True
        return _ModelEvidence()

    monkeypatch.setattr(subject, "resolve_prospective_model_compute_money", resolver)

    class WrongPlan(_Plan):
        intent_ids = ("other-intent",)

    monkeypatch.setattr(subject, "PortfolioPlan", WrongPlan)
    with pytest.raises(subject.ProspectiveApplicableCostError, match="intent_id/intent_sha256"):
        subject.resolve_prospective_applicable_costs(
            intent=_Intent(),
            plan=WrongPlan(),
            router_store=_Store(),
            model_request_id="request-729",
            decision_at=DECISION_AT,
        )
    assert called is False


def test_subclass_authorities_are_rejected(monkeypatch):
    _patch_types(monkeypatch)

    class IntentSubclass(_Intent):
        pass

    class PlanSubclass(_Plan):
        pass

    class StoreSubclass(_Store):
        pass

    cases = (
        (IntentSubclass(), _Plan(), _Store(), "OpportunityIntent"),
        (_Intent(), PlanSubclass(), _Store(), "PortfolioPlan"),
        (_Intent(), _Plan(), StoreSubclass(), "ModelComputeRouterStore"),
    )
    monkeypatch.setattr(
        subject,
        "resolve_prospective_model_compute_money",
        lambda **_kwargs: pytest.fail("authority read must not happen"),
    )
    for intent, plan, store, error in cases:
        with pytest.raises(subject.ProspectiveApplicableCostError, match=error):
            subject.resolve_prospective_applicable_costs(
                intent=intent,
                plan=plan,
                router_store=store,
                model_request_id="request-729",
                decision_at=DECISION_AT,
            )


def test_duplicate_or_missing_required_class_is_rejected(monkeypatch):
    result, _ = _resolve(monkeypatch)
    with pytest.raises(subject.ProspectiveApplicableCostError, match="each required cost class exactly once"):
        subject.ProspectiveApplicableCostResolution(
            intent_sha256=result.intent_sha256,
            opportunity_id=result.opportunity_id,
            portfolio_plan_sha256=result.portfolio_plan_sha256,
            decision_at=result.decision_at,
            components=result.components[:-1] + (result.components[0],),
        )


def test_source_or_dependency_axis_changes_proof_identity(monkeypatch):
    first, _ = _resolve(monkeypatch, _ModelEvidence(evidence_id="b" * 64))
    second, _ = _resolve(monkeypatch, _ModelEvidence(evidence_id="c" * 64))
    assert first.evidence_id != second.evidence_id

    fee = next(item for item in first.components if item.cost_class is CostClass.EXECUTION_FEES_COMMISSION_TAX)
    altered_fee = subject.ProspectiveApplicableCostComponent(
        cost_class=fee.cost_class,
        status=subject.ProspectiveCostResolutionStatus.TERMINAL_STATE_DEPENDENT,
        dependency_axes=(subject.ProspectiveCostDependencyAxis.TERMINAL_STATE,),
        reason=fee.reason,
    )
    altered_components = tuple(
        altered_fee if item.cost_class is fee.cost_class else item for item in first.components
    )
    altered = subject.ProspectiveApplicableCostResolution(
        intent_sha256=first.intent_sha256,
        opportunity_id=first.opportunity_id,
        portfolio_plan_sha256=first.portfolio_plan_sha256,
        decision_at=first.decision_at,
        components=altered_components,
    )
    assert altered.evidence_id != first.evidence_id


def _load_real_fixture():
    impl_path = Path(__file__).with_name("_test_portfolio_plan_impl.py")
    spec = importlib.util.spec_from_file_location("_applicable_cost_real_fixture", impl_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_types_compose_through_actual_router_and_model_money_resolver():
    module = _load_real_fixture()
    goal = module.PortfolioPlanTests._goal()
    policy = module.PortfolioPlanTests._policy(goal)
    book = PaperBook("1000")
    intent = module.PortfolioPlanTests._intent(
        goal,
        suffix="aggregate-real",
        strategy_class=module.StrategyClass.PREDICTIVE_EDGE,
    )
    graph = module.PortfolioPlanTests._graph(book, (intent,))
    plan = module.build_portfolio_plan(
        book,
        (intent,),
        policy,
        module.PortfolioPlanTests.DECISION_TS,
        dependency_graph=graph,
    )
    proposal_ts = intent.risk_context.proposal_ts
    assert proposal_ts is not None
    decision_at = datetime.fromisoformat(proposal_ts.replace("Z", "+00:00"))

    with tempfile.TemporaryDirectory() as tmp:
        store = ModelComputeRouterStore(Path(tmp) / "router.json")
        candidate = ComputeCandidate(
            candidate_id="aggregate-deterministic",
            tier=ComputeTier.DETERMINISTIC,
            backend_id="aggregate-engine",
            model_id="no-model",
            config_sha256="c" * 64,
            capabilities=("intent-economics",),
            estimated_cost=Decimal("0"),
            estimated_latency_seconds=Decimal("0"),
        )
        request = ComputeRouteRequest(
            request_id="aggregate-real-route",
            created_at=intent.evidence.observed_at,
            decision_deadline=proposal_ts,
            required_capability="intent-economics",
            data_classification=DataClassification.PUBLIC,
            allow_cloud=False,
            max_cost=Decimal("0"),
            response_ttl_seconds=Decimal("1"),
            baseline_candidate_id=candidate.candidate_id,
            decision_input_sha256=intent.intent_sha256,
            decision_evidence_sha256=intent.evidence.evidence_sha256,
        )
        store.route(
            request,
            (candidate,),
            ComputeRoutingPolicy(policy_id="aggregate-cost-policy", policy_version=1),
            as_of=intent.evidence.observed_at,
        )
        result = subject.resolve_prospective_applicable_costs(
            intent=intent,
            plan=plan,
            router_store=store,
            model_request_id=request.request_id,
            decision_at=decision_at,
        )

    assert type(result) is subject.ProspectiveApplicableCostResolution
    assert result.intent_sha256 == intent.intent_sha256
    assert result.portfolio_plan_sha256 == plan.plan_sha256
    assert result.completeness is subject.ProspectiveApplicableCostCompleteness.INCOMPLETE
    model = next(item for item in result.components if item.cost_class is CostClass.MODEL_COMPUTE_AI)
    assert model.status is subject.ProspectiveCostResolutionStatus.UNKNOWN_UNPROVEN
    assert model.source_evidence_id is not None
