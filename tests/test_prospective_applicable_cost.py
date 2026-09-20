from __future__ import annotations

from dataclasses import replace
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
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.opportunity import (
    Opportunity,
    OpportunityDecision,
    QuoteRef,
    StrategyClass,
)
from autosport.portfolio_plan import (
    EvidenceTruth,
    OpportunityEvidence,
    OpportunityIntent,
)
from autosport.prospective_applicable_cost import (
    CostApplicabilityEvidence,
    ProspectiveApplicableCostError,
    ProspectiveApplicableCostResolution,
    ProspectiveCostCompleteness,
    ProspectiveCostEvidence,
    resolve_prospective_applicable_costs,
)
from autosport.risk import ProposedTicketRiskContext


DECISION_AT = datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc)
DECISION_TEXT = "2026-09-20T22:00:00+00:00"


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-prospective-cost",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="EUR",
        max_stake_fraction=Decimal("0.10"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1000"),
        max_risk_of_ruin=Decimal("1"),
        max_execution_slippage_fraction=Decimal("1"),
        max_quote_age_seconds=Decimal("3600"),
        max_concurrent_positions=10,
    )


def _intent(*, intent_id: str = "intent-prospective-cost") -> OpportunityIntent:
    goal = _goal()
    legs = (
        TicketLeg(
            "event-1",
            "market-1",
            "home",
            Decimal("2.10"),
            sport="soccer",
        ),
        TicketLeg(
            "event-1",
            "market-1",
            "away",
            Decimal("2.10"),
            sport="soccer",
        ),
    )
    quotes = (
        MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="home",
            decimal_odds=Decimal("2.10"),
            observed_ts="2026-09-20T21:59:59+00:00",
            source_id="provider-a",
            sequence=1,
            source_ts="2026-09-20T21:59:59+00:00",
            ingest_ts="2026-09-20T21:59:59+00:00",
            sport="soccer",
        ),
        MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="away",
            decimal_odds=Decimal("2.10"),
            observed_ts="2026-09-20T21:59:59+00:00",
            source_id="provider-b",
            sequence=1,
            source_ts="2026-09-20T21:59:59+00:00",
            ingest_ts="2026-09-20T21:59:59+00:00",
            sport="soccer",
        ),
    )
    risk_context = ProposedTicketRiskContext(
        legs=legs,
        quotes=quotes,
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
        proposal_ts=DECISION_TEXT,
    )
    opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=tuple(
            QuoteRef.from_market_event(
                quote,
                market_snapshot_hash="9" * 64,
            )
            for quote in quotes
        ),
    )
    evidence = OpportunityEvidence(
        evidence_id="prospective-cost-evidence",
        observed_at="2026-09-20T21:59:59+00:00",
        causal_cutoff="2026-09-20T21:59:58+00:00",
        reproducibility_sha256="a" * 64,
        truth=EvidenceTruth.EXACT,
        outcome_space_complete=True,
        terminal_state_space_sha256="b" * 64,
        execution_assumptions_sha256="c" * 64,
        execution_feasible=True,
    )
    return OpportunityIntent(
        intent_id=intent_id,
        opportunity=opportunity,
        evidence=evidence,
        risk_context=risk_context,
        signal_strength=Decimal("0.05"),
        strategy_id="strategy-prospective-cost",
        config_sha256="d" * 64,
    )


def _source(label: str) -> CostSourceRef:
    return CostSourceRef(
        family="prospective-test-assertion",
        evidence_id=label,
        sha256=hashlib.sha256(label.encode("utf-8")).hexdigest(),
    )


def _applicability(
    intent: OpportunityIntent,
    cost_class: CostClass,
    *,
    applicable: bool = True,
    available_at: datetime | None = None,
    valid_until: datetime | None = None,
    basis: CostBasis = CostBasis.AUTHORITATIVE_DECLARATION,
) -> CostApplicabilityEvidence:
    available = available_at or (DECISION_AT - timedelta(seconds=1))
    return CostApplicabilityEvidence(
        intent_sha256=intent.intent_sha256,
        opportunity_id=intent.opportunity.opportunity_id,
        cost_class=cost_class,
        applicable=applicable,
        basis=basis,
        source=_source(f"applicability-{cost_class.value}-{applicable}"),
        observed_at=available - timedelta(seconds=1),
        available_at=available,
        valid_until=valid_until or (DECISION_AT + timedelta(minutes=5)),
    )


def _cost(
    intent: OpportunityIntent,
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
        intent_sha256=intent.intent_sha256,
        opportunity_id=intent.opportunity.opportunity_id,
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


def _candidate_inputs(
    intent: OpportunityIntent,
) -> tuple[tuple[CostApplicabilityEvidence, ...], tuple[ProspectiveCostEvidence, ...]]:
    applicability = tuple(
        _applicability(intent, cost_class)
        for cost_class in REQUIRED_COST_CLASSES
    )
    costs = tuple(_cost(intent, cost_class) for cost_class in REQUIRED_COST_CLASSES)
    return applicability, costs


def _resolve(
    intent: OpportunityIntent,
    applicability: tuple[CostApplicabilityEvidence, ...],
    costs: tuple[ProspectiveCostEvidence, ...],
    *,
    decision_at: datetime = DECISION_AT,
) -> ProspectiveApplicableCostResolution:
    return resolve_prospective_applicable_costs(
        intent=intent,
        decision_at=decision_at,
        applicability=applicability,
        costs=costs,
    )


def test_exact_caller_candidates_remain_incomplete_until_product_authority_exists() -> None:
    intent = _intent()
    applicability, costs = _candidate_inputs(intent)

    result = _resolve(intent, applicability, costs)

    assert result.completeness is ProspectiveCostCompleteness.INCOMPLETE
    assert result.total_subtractable_amount is None
    assert result.intent_sha256 == intent.intent_sha256
    assert result.opportunity_id == intent.opportunity.opportunity_id
    assert result.currency == "EUR"
    assert len(result.incomplete_reasons) == len(REQUIRED_COST_CLASSES)
    for cost_class in REQUIRED_COST_CLASSES:
        assert (
            f"product-owned-cost-source-unresolved:{cost_class.value}"
            in result.incomplete_reasons
        )
    assert result.to_dict()["proof_id"] == result.proof_id


def test_resolution_verdict_cannot_be_directly_minted_by_caller() -> None:
    with pytest.raises(
        ProspectiveApplicableCostError,
        match="created only by resolve_prospective_applicable_costs",
    ):
        ProspectiveApplicableCostResolution()


def test_caller_forged_zero_cost_does_not_create_complete_authority() -> None:
    intent = _intent()
    applicability, costs = _candidate_inputs(intent)
    target = REQUIRED_COST_CLASSES[0]
    costs = tuple(
        (
            _cost(
                intent,
                target,
                amount=Decimal("0"),
                truth=CostTruth.KNOWN_ZERO,
            )
            if item.cost_class is target
            else item
        )
        for item in costs
    )

    result = _resolve(intent, applicability, costs)

    assert result.completeness is ProspectiveCostCompleteness.INCOMPLETE
    assert result.total_subtractable_amount is None
    assert (
        f"product-owned-cost-source-unresolved:{target.value}"
        in result.incomplete_reasons
    )


def test_caller_forged_not_applicable_does_not_erase_cost_class() -> None:
    intent = _intent()
    applicability, costs = _candidate_inputs(intent)
    target = REQUIRED_COST_CLASSES[0]
    applicability = tuple(
        (
            _applicability(intent, target, applicable=False)
            if item.cost_class is target
            else item
        )
        for item in applicability
    )
    costs = tuple(item for item in costs if item.cost_class is not target)

    result = _resolve(intent, applicability, costs)

    assert result.completeness is ProspectiveCostCompleteness.INCOMPLETE
    assert (
        f"product-owned-applicability-unresolved:{target.value}"
        in result.incomplete_reasons
    )


def test_missing_applicability_fails_closed() -> None:
    intent = _intent()
    applicability, costs = _candidate_inputs(intent)
    target = REQUIRED_COST_CLASSES[-1]

    result = _resolve(
        intent,
        tuple(item for item in applicability if item.cost_class is not target),
        costs,
    )

    assert f"missing-applicability:{target.value}" in result.incomplete_reasons
    assert result.total_subtractable_amount is None


def test_non_authoritative_zero_is_rejected_as_positive_cost_basis() -> None:
    intent = _intent()
    applicability, costs = _candidate_inputs(intent)
    target = REQUIRED_COST_CLASSES[0]
    costs = tuple(
        (
            _cost(
                intent,
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

    result = _resolve(intent, applicability, costs)

    assert (
        f"non-authoritative-cost-basis:{target.value}"
        in result.incomplete_reasons
    )


def test_not_applicable_requires_authoritative_declaration_syntax() -> None:
    intent = _intent()
    with pytest.raises(
        ProspectiveApplicableCostError,
        match="applicability requires AUTHORITATIVE_DECLARATION basis",
    ):
        _applicability(
            intent,
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
def test_cost_falsifiers_are_explicit(
    mutation: str,
    expected_reason: str,
) -> None:
    intent = _intent()
    applicability, costs = _candidate_inputs(intent)
    target = REQUIRED_COST_CLASSES[0]
    replacement = {
        "currency": _cost(intent, target, currency="USD"),
        "stale": _cost(
            intent,
            target,
            valid_until=DECISION_AT - timedelta(seconds=1),
        ),
        "informational": _cost(
            intent,
            target,
            treatment=CostTreatment.INFORMATIONAL,
        ),
    }[mutation]
    costs = tuple(
        replacement if item.cost_class is target else item for item in costs
    )

    result = _resolve(intent, applicability, costs)

    assert f"{expected_reason}:{target.value}" in result.incomplete_reasons
    assert result.total_subtractable_amount is None


def test_duplicate_cost_authority_is_a_deterministic_falsifier() -> None:
    intent = _intent()
    applicability, costs = _candidate_inputs(intent)
    target = REQUIRED_COST_CLASSES[0]
    duplicate = _cost(intent, target, suffix="second")

    result = _resolve(intent, applicability, (*costs, duplicate))

    assert f"conflicting-cost:{target.value}" in result.incomplete_reasons
    assert result.total_subtractable_amount is None


def test_cross_intent_evidence_fails_closed() -> None:
    intent = _intent()
    other = _intent(intent_id="other-intent")
    applicability, costs = _candidate_inputs(intent)
    target = REQUIRED_COST_CLASSES[0]
    forged = _cost(other, target)
    costs = tuple(forged if item.cost_class is target else item for item in costs)

    result = _resolve(intent, applicability, costs)

    assert f"cost-intent-mismatch:{target.value}" in result.incomplete_reasons
    assert result.total_subtractable_amount is None


def test_future_applicability_fails_closed() -> None:
    intent = _intent()
    applicability, costs = _candidate_inputs(intent)
    target = REQUIRED_COST_CLASSES[0]
    future = _applicability(
        intent,
        target,
        available_at=DECISION_AT + timedelta(seconds=1),
        valid_until=DECISION_AT + timedelta(minutes=5),
    )
    applicability = tuple(
        future if item.cost_class is target else item for item in applicability
    )

    result = _resolve(intent, applicability, costs)

    assert (
        f"stale-or-future-applicability:{target.value}"
        in result.incomplete_reasons
    )


def test_decision_before_intent_proposal_is_rejected() -> None:
    intent = _intent()
    applicability, costs = _candidate_inputs(intent)

    with pytest.raises(ProspectiveApplicableCostError, match="cannot precede"):
        _resolve(
            intent,
            applicability,
            costs,
            decision_at=DECISION_AT - timedelta(seconds=1),
        )


def test_proof_identity_changes_with_decision_time() -> None:
    intent = _intent()
    applicability, costs = _candidate_inputs(intent)

    first = _resolve(intent, applicability, costs)
    second = _resolve(
        intent,
        applicability,
        costs,
        decision_at=DECISION_AT + timedelta(seconds=1),
    )

    assert first.proof_id != second.proof_id


def test_intent_subclass_spoofing_is_rejected() -> None:
    intent = _intent()

    class ForgedIntent(OpportunityIntent):
        pass

    forged = ForgedIntent(
        intent_id=intent.intent_id,
        opportunity=intent.opportunity,
        evidence=intent.evidence,
        risk_context=intent.risk_context,
        signal_strength=intent.signal_strength,
        strategy_id=intent.strategy_id,
        config_sha256=intent.config_sha256,
        model_id=intent.model_id,
    )
    applicability, costs = _candidate_inputs(intent)

    with pytest.raises(ProspectiveApplicableCostError, match="exact canonical"):
        resolve_prospective_applicable_costs(
            intent=forged,
            decision_at=DECISION_AT,
            applicability=applicability,
            costs=costs,
        )
