from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib

import pytest

from autosport.campaign_cost_evidence import (
    CostBasis,
    CostClass,
    CostSourceRef,
    CostTreatment,
    CostTruth,
    CostUnit,
    REQUIRED_COST_CLASSES,
)
from autosport.opportunity import OpportunitySet, PortfolioPlan
from autosport.prospective_applicable_cost import (
    CostApplicabilityEvidence,
    ProspectiveApplicableCostError,
    ProspectiveApplicableCostResolution,
    ProspectiveCostCompleteness,
    ProspectiveCostEvidence,
    resolve_prospective_applicable_costs,
)


DECISION_AT = datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc)


def _plan() -> PortfolioPlan:
    return PortfolioPlan(opportunity_set=OpportunitySet(opportunities=()))


def _source(label: str) -> CostSourceRef:
    return CostSourceRef(
        family="prospective-test-authority",
        evidence_id=label,
        sha256=hashlib.sha256(label.encode("utf-8")).hexdigest(),
    )


def _applicability(
    plan: PortfolioPlan,
    cost_class: CostClass,
    *,
    applicable: bool = True,
    available_at: datetime | None = None,
    valid_until: datetime | None = None,
    basis: CostBasis = CostBasis.AUTHORITATIVE_DECLARATION,
) -> CostApplicabilityEvidence:
    available = available_at or (DECISION_AT - timedelta(seconds=1))
    return CostApplicabilityEvidence(
        plan_id=plan.plan_id,
        cost_class=cost_class,
        applicable=applicable,
        basis=basis,
        source=_source(f"applicability-{cost_class.value}-{applicable}"),
        observed_at=available - timedelta(seconds=1),
        available_at=available,
        valid_until=valid_until or (DECISION_AT + timedelta(minutes=5)),
    )


def _cost(
    plan: PortfolioPlan,
    cost_class: CostClass,
    *,
    amount: Decimal = Decimal("1"),
    truth: CostTruth = CostTruth.KNOWN_AMOUNT,
    basis: CostBasis = CostBasis.AUTHORITATIVE_DECLARATION,
    treatment: CostTreatment = CostTreatment.SUBTRACT_FROM_GROSS,
    currency: str = "EUR",
    available_at: datetime | None = None,
    valid_until: datetime | None = None,
    suffix: str = "",
) -> ProspectiveCostEvidence:
    available = available_at or (DECISION_AT - timedelta(seconds=1))
    return ProspectiveCostEvidence(
        plan_id=plan.plan_id,
        cost_class=cost_class,
        truth=truth,
        basis=basis,
        treatment=treatment,
        source=_source(f"cost-{cost_class.value}-{suffix}"),
        unit=CostUnit.MONEY,
        currency=currency,
        amount=amount,
        observed_at=available - timedelta(seconds=1),
        available_at=available,
        valid_until=valid_until or (DECISION_AT + timedelta(minutes=5)),
    )


def _complete_inputs(
    plan: PortfolioPlan,
) -> tuple[tuple[CostApplicabilityEvidence, ...], tuple[ProspectiveCostEvidence, ...]]:
    applicability = tuple(
        _applicability(plan, cost_class)
        for cost_class in REQUIRED_COST_CLASSES
    )
    costs = tuple(_cost(plan, cost_class) for cost_class in REQUIRED_COST_CLASSES)
    return applicability, costs


def test_complete_resolution_is_derived_and_sums_only_subtractive_costs() -> None:
    plan = _plan()
    applicability, _ = _complete_inputs(plan)
    embedded_class = REQUIRED_COST_CLASSES[0]
    costs = tuple(
        _cost(
            plan,
            cost_class,
            treatment=(
                CostTreatment.EMBEDDED_IN_GROSS
                if cost_class is embedded_class
                else CostTreatment.SUBTRACT_FROM_GROSS
            ),
        )
        for cost_class in REQUIRED_COST_CLASSES
    )

    result = resolve_prospective_applicable_costs(
        plan=plan,
        decision_at=DECISION_AT,
        currency="EUR",
        applicability=applicability,
        costs=costs,
    )

    assert result.completeness is ProspectiveCostCompleteness.COMPLETE
    assert result.total_subtractable_amount == Decimal(
        len(REQUIRED_COST_CLASSES) - 1
    )
    assert result.incomplete_reasons == ()
    assert result.plan_id == plan.plan_id
    assert result.opportunity_set_id == plan.opportunity_set.opportunity_set_id
    assert result.to_dict()["proof_id"] == result.proof_id


def test_resolution_verdict_cannot_be_directly_minted_by_caller() -> None:
    with pytest.raises(
        ProspectiveApplicableCostError,
        match="created only by resolve_prospective_applicable_costs",
    ):
        ProspectiveApplicableCostResolution()


def test_missing_applicability_fails_closed_and_withholds_total() -> None:
    plan = _plan()
    applicability, costs = _complete_inputs(plan)
    missing_class = REQUIRED_COST_CLASSES[-1]

    result = resolve_prospective_applicable_costs(
        plan=plan,
        decision_at=DECISION_AT,
        currency="EUR",
        applicability=tuple(
            item for item in applicability if item.cost_class is not missing_class
        ),
        costs=costs,
    )

    assert result.completeness is ProspectiveCostCompleteness.INCOMPLETE
    assert result.total_subtractable_amount is None
    assert f"missing-applicability:{missing_class.value}" in result.incomplete_reasons


def test_non_authoritative_known_zero_does_not_upgrade_completeness() -> None:
    plan = _plan()
    applicability, costs = _complete_inputs(plan)
    target = REQUIRED_COST_CLASSES[0]
    costs = tuple(
        (
            _cost(
                plan,
                target,
                amount=Decimal("0"),
                truth=CostTruth.KNOWN_ZERO,
                basis=CostBasis.CONFIGURED_ESTIMATE,
            )
            if item.cost_class is target
            else item
        )
        for item in costs
    )

    result = resolve_prospective_applicable_costs(
        plan=plan,
        decision_at=DECISION_AT,
        currency="EUR",
        applicability=applicability,
        costs=costs,
    )

    assert result.completeness is ProspectiveCostCompleteness.INCOMPLETE
    assert (
        f"non-authoritative-cost-basis:{target.value}"
        in result.incomplete_reasons
    )


def test_not_applicable_claim_requires_authoritative_applicability_basis() -> None:
    plan = _plan()
    with pytest.raises(
        ProspectiveApplicableCostError,
        match="applicability requires AUTHORITATIVE_DECLARATION basis",
    ):
        _applicability(
            plan,
            REQUIRED_COST_CLASSES[0],
            applicable=False,
            basis=CostBasis.CONFIGURED_ESTIMATE,
        )


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("currency", "currency-mismatch"),
        ("stale", "stale-or-future-cost"),
        ("informational", "non-economic-treatment"),
    ],
)
def test_cost_falsifiers_fail_closed(
    mutation: str,
    expected_reason: str,
) -> None:
    plan = _plan()
    applicability, costs = _complete_inputs(plan)
    target = REQUIRED_COST_CLASSES[0]
    replacement = {
        "currency": _cost(plan, target, currency="USD"),
        "stale": _cost(
            plan,
            target,
            valid_until=DECISION_AT - timedelta(seconds=1),
        ),
        "informational": _cost(
            plan,
            target,
            treatment=CostTreatment.INFORMATIONAL,
        ),
    }[mutation]
    costs = tuple(
        replacement if item.cost_class is target else item for item in costs
    )

    result = resolve_prospective_applicable_costs(
        plan=plan,
        decision_at=DECISION_AT,
        currency="EUR",
        applicability=applicability,
        costs=costs,
    )

    assert result.completeness is ProspectiveCostCompleteness.INCOMPLETE
    assert result.total_subtractable_amount is None
    assert f"{expected_reason}:{target.value}" in result.incomplete_reasons


def test_duplicate_cost_authority_is_a_deterministic_falsifier() -> None:
    plan = _plan()
    applicability, costs = _complete_inputs(plan)
    target = REQUIRED_COST_CLASSES[0]
    duplicate = _cost(plan, target, suffix="second")

    result = resolve_prospective_applicable_costs(
        plan=plan,
        decision_at=DECISION_AT,
        currency="EUR",
        applicability=applicability,
        costs=(*costs, duplicate),
    )

    assert result.completeness is ProspectiveCostCompleteness.INCOMPLETE
    assert f"conflicting-cost:{target.value}" in result.incomplete_reasons
    assert result.total_subtractable_amount is None


def test_non_applicable_cost_class_must_not_also_carry_cost_evidence() -> None:
    plan = _plan()
    applicability, costs = _complete_inputs(plan)
    target = REQUIRED_COST_CLASSES[0]
    applicability = tuple(
        (
            _applicability(plan, target, applicable=False)
            if item.cost_class is target
            else item
        )
        for item in applicability
    )

    result = resolve_prospective_applicable_costs(
        plan=plan,
        decision_at=DECISION_AT,
        currency="EUR",
        applicability=applicability,
        costs=costs,
    )

    assert result.completeness is ProspectiveCostCompleteness.INCOMPLETE
    assert f"cost-for-not-applicable:{target.value}" in result.incomplete_reasons


def test_proof_identity_changes_with_decision_time() -> None:
    plan = _plan()
    applicability, costs = _complete_inputs(plan)

    first = resolve_prospective_applicable_costs(
        plan=plan,
        decision_at=DECISION_AT,
        currency="EUR",
        applicability=applicability,
        costs=costs,
    )
    second = resolve_prospective_applicable_costs(
        plan=plan,
        decision_at=DECISION_AT + timedelta(seconds=1),
        currency="EUR",
        applicability=applicability,
        costs=costs,
    )

    assert first.completeness is ProspectiveCostCompleteness.COMPLETE
    assert second.completeness is ProspectiveCostCompleteness.COMPLETE
    assert first.proof_id != second.proof_id
