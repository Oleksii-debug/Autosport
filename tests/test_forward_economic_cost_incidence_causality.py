from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.forward_economic_evidence import (
    AlphaAllocation,
    BetSide,
    EconomicCostCausality,
    FamilywiseAlphaRegistry,
    ForwardDecisionObservation,
    ForwardEconomicEvidenceAccumulator,
    ForwardEconomicEvidenceError,
    ForwardEconomicProtocol,
    ResolvedPolicyOutcome,
)


T0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
EVENT_SHA = "a" * 64
DECISION_SHA = "b" * 64
COST_EVIDENCE_SHA = "c" * 64
ALLOCATION_AUTHORITY_SHA = "d" * 64
CHAMPION_DECISION_SHA = "e" * 64
RESOLVER_AUTHORITY_SHA = "f" * 64


def _cost_only_outcome(
    *,
    incurred_at: datetime,
    causality: EconomicCostCausality = EconomicCostCausality.DECISION_CAUSED,
    cost_class_id: str | None = None,
    allocation_authority_sha256: str | None = None,
    available_at: datetime | None = None,
) -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=0,
        universe_event_sha256=EVENT_SHA,
        decision_sha256=DECISION_SHA,
        decision_committed_at=T0,
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal("-2"),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
        wager_pnl_currency=Decimal("0"),
        economic_cost_currency=Decimal("2"),
        economic_cost_evidence_sha256=COST_EVIDENCE_SHA,
        economic_cost_incurred_at=incurred_at,
        economic_cost_available_at=(
            T0 + timedelta(minutes=1)
            if available_at is None
            else available_at
        ),
        economic_cost_causality=causality,
        economic_cost_class_id=cost_class_id,
        economic_cost_allocation_authority_sha256=allocation_authority_sha256,
    )


def test_economic_cost_incidence_cannot_precede_committed_decision() -> None:
    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="economic cost cannot be incurred before the committed decision",
    ):
        _cost_only_outcome(incurred_at=T0 - timedelta(microseconds=1))


def test_economic_cost_incidence_at_committed_decision_remains_admissible() -> None:
    outcome = _cost_only_outcome(incurred_at=T0)

    assert outcome.economic_cost_incurred_at == T0
    assert outcome.economic_cost_available_at == T0 + timedelta(minutes=1)
    assert outcome.effective_wager_pnl_currency == Decimal("0")
    assert outcome.net_pnl_currency == Decimal("-2")



def test_preexisting_shared_cost_may_precede_decision_only_with_allocation_authority() -> None:
    outcome = _cost_only_outcome(
        incurred_at=T0 - timedelta(hours=3),
        causality=EconomicCostCausality.PREEXISTING_SHARED,
        cost_class_id="provider.subscription.daily",
        allocation_authority_sha256=ALLOCATION_AUTHORITY_SHA,
    )

    assert outcome.economic_cost_incurred_at == T0 - timedelta(hours=3)
    assert outcome.economic_cost_available_at == T0 + timedelta(minutes=1)
    assert outcome.economic_cost_causality is EconomicCostCausality.PREEXISTING_SHARED
    assert outcome.economic_cost_class_id == "provider.subscription.daily"
    assert (
        outcome.economic_cost_allocation_authority_sha256
        == ALLOCATION_AUTHORITY_SHA
    )


@pytest.mark.parametrize(
    ("cost_class_id", "allocation_authority_sha256"),
    [
        (None, ALLOCATION_AUTHORITY_SHA),
        ("provider.subscription.daily", None),
        (None, None),
    ],
)
def test_preexisting_shared_cost_requires_exact_class_and_allocation_authority(
    cost_class_id: str | None,
    allocation_authority_sha256: str | None,
) -> None:
    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="PREEXISTING_SHARED requires canonical cost class and allocation authority",
    ):
        _cost_only_outcome(
            incurred_at=T0 - timedelta(hours=3),
            causality=EconomicCostCausality.PREEXISTING_SHARED,
            cost_class_id=cost_class_id,
            allocation_authority_sha256=allocation_authority_sha256,
        )


def test_decision_caused_cost_cannot_smuggle_shared_allocation_metadata() -> None:
    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="shared cost allocation metadata requires PREEXISTING_SHARED causality",
    ):
        _cost_only_outcome(
            incurred_at=T0,
            cost_class_id="provider.subscription.daily",
            allocation_authority_sha256=ALLOCATION_AUTHORITY_SHA,
        )


def test_preexisting_shared_cost_keeps_evidence_after_incidence_clock_law() -> None:
    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="economic cost evidence cannot become available before cost incidence",
    ):
        _cost_only_outcome(
            incurred_at=T0 - timedelta(hours=1),
            available_at=T0 - timedelta(hours=2),
            causality=EconomicCostCausality.PREEXISTING_SHARED,
            cost_class_id="provider.subscription.daily",
            allocation_authority_sha256=ALLOCATION_AUTHORITY_SHA,
        )


class _Resolver:
    def __init__(
        self,
        challenger: ResolvedPolicyOutcome,
        champion: ResolvedPolicyOutcome,
    ) -> None:
        self._outcomes = {
            "challenger": challenger,
            "champion": champion,
        }

    @property
    def authority_sha256(self) -> str:
        return RESOLVER_AUTHORITY_SHA

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        outcome = self._outcomes[policy_id]
        assert outcome.sequence == sequence
        assert outcome.universe_event_sha256 == universe_event_sha256
        assert outcome.decision_sha256 == decision_sha256
        return outcome


def _accumulator() -> ForwardEconomicEvidenceAccumulator:
    alpha = FamilywiseAlphaRegistry(
        family_id="shared-cost-causality",
        total_alpha=Decimal("0.05"),
        allocations=(AlphaAllocation("challenger", Decimal("0.025")),),
        sealed_at=T0 - timedelta(minutes=2),
    )
    protocol = ForwardEconomicProtocol(
        protocol_id="shared-cost-causality-v1",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="universe",
        universe_sha256="1" * 64,
        authority_binding_sha256="2" * 64,
        alpha_registry=alpha,
        minimum_events=1,
        risk_unit_currency=Decimal("10"),
        maximum_accepted_odds=Decimal("20"),
        maximum_drawdown_currency=Decimal("100"),
        maximum_economic_cost_currency=Decimal("10"),
        absolute_lambda=Decimal("0.1"),
        paired_lambda=Decimal("0.1"),
        start_sequence=0,
        frozen_at=T0 - timedelta(minutes=1),
    )
    return ForwardEconomicEvidenceAccumulator(protocol)


def _champion_none_outcome() -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id="champion",
        sequence=0,
        universe_event_sha256=EVENT_SHA,
        decision_sha256=CHAMPION_DECISION_SHA,
        decision_committed_at=T0,
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal("0"),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
    )


def _record_shared_cost(
    allocation_authority_sha256: str,
):
    accumulator = _accumulator()
    challenger = _cost_only_outcome(
        incurred_at=T0 - timedelta(hours=3),
        causality=EconomicCostCausality.PREEXISTING_SHARED,
        cost_class_id="provider.subscription.daily",
        allocation_authority_sha256=allocation_authority_sha256,
    )
    step = accumulator.record(
        ForwardDecisionObservation(
            sequence=0,
            universe_sha256="1" * 64,
            universe_event_sha256=EVENT_SHA,
            challenger_decision_sha256=DECISION_SHA,
            champion_decision_sha256=CHAMPION_DECISION_SHA,
        ),
        _Resolver(challenger, _champion_none_outcome()),
    )
    return step, accumulator.summary()


def test_shared_cost_allocation_identity_is_committed_into_step_and_evidence_hash() -> None:
    step_a, summary_a = _record_shared_cost(ALLOCATION_AUTHORITY_SHA)
    payload = step_a.to_payload()

    assert (
        payload["challenger_economic_cost_causality"]
        == EconomicCostCausality.PREEXISTING_SHARED.value
    )
    assert payload["challenger_economic_cost_class_id"] == "provider.subscription.daily"
    assert (
        payload["challenger_economic_cost_allocation_authority_sha256"]
        == ALLOCATION_AUTHORITY_SHA
    )

    step_b, summary_b = _record_shared_cost("9" * 64)
    assert (
        step_b.challenger_economic_cost_allocation_authority_sha256
        == "9" * 64
    )
    assert summary_a.evidence_sha256 != summary_b.evidence_sha256
