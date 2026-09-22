from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext

import pytest

from autosport.forward_economic_evidence import (
    AlphaAllocation,
    BetSide,
    FamilywiseAlphaRegistry,
    ForwardDecisionObservation,
    ForwardEconomicEvidenceAccumulator,
    ForwardEconomicEvidenceError,
    ForwardEconomicProtocol,
    ResolvedPolicyOutcome,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 22, 20, 0, tzinfo=UTC)
UNIVERSE_SHA = "a" * 64
AUTHORITY_SHA = "b" * 64
EXECUTION_SHA = "c" * 64
SETTLEMENT_SHA = "d" * 64
COST_SHA = "e" * 64


def _executed_outcome(
    *,
    net_pnl: str,
    wager_pnl: str,
    cost: str,
) -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=0,
        universe_event_sha256="1" * 64,
        decision_sha256="2" * 64,
        decision_committed_at=T0 + timedelta(minutes=2),
        side=BetSide.BACK,
        accepted_odds=Decimal("2"),
        accepted_stake=Decimal("10"),
        net_pnl_currency=Decimal(net_pnl),
        execution_evidence_sha256=EXECUTION_SHA,
        execution_accepted_at=T0 + timedelta(minutes=3),
        settlement_evidence_sha256=SETTLEMENT_SHA,
        settlement_available_at=T0 + timedelta(minutes=5),
        wager_pnl_currency=Decimal(wager_pnl),
        economic_cost_currency=Decimal(cost),
        economic_cost_evidence_sha256=COST_SHA,
        economic_cost_available_at=T0 + timedelta(minutes=6),
    )


def test_exact_all_in_relation_accepts_valid_value_under_hostile_context() -> None:
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_UP
        outcome = _executed_outcome(
            net_pnl="1.234567890123456783",
            wager_pnl="1.234567890123456789",
            cost="0.000000000000000006",
        )

    assert outcome.net_pnl_currency == Decimal("1.234567890123456783")
    assert outcome.effective_wager_pnl_currency == Decimal(
        "1.234567890123456789"
    )


def test_rounded_alias_cannot_satisfy_exact_all_in_relation() -> None:
    # Under Decimal precision 6 the old implementation rounded
    # wager_pnl - cost to 1.23457 and therefore accepted this false identity.
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_UP
        with pytest.raises(
            ForwardEconomicEvidenceError,
            match="all-in net P&L must equal wager P&L minus economic cost",
        ):
            _executed_outcome(
                net_pnl="1.23457",
                wager_pnl="1.234567890123456789",
                cost="0.000000000000000006",
            )


class _Resolver:
    authority_sha256 = AUTHORITY_SHA

    def __init__(
        self,
        challenger: ResolvedPolicyOutcome,
        champion: ResolvedPolicyOutcome,
    ) -> None:
        self._rows = {
            "challenger": challenger,
            "champion": champion,
        }

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        return self._rows[policy_id]


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="decimal-context-family",
        total_alpha=Decimal("0.2"),
        allocations=(AlphaAllocation("challenger", Decimal("0.2")),),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="decimal-context-protocol",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="decimal-context-universe",
        universe_sha256=UNIVERSE_SHA,
        authority_binding_sha256=AUTHORITY_SHA,
        alpha_registry=registry,
        minimum_events=1,
        risk_unit_currency=Decimal("200000000"),
        maximum_accepted_odds=Decimal("10"),
        maximum_drawdown_currency=Decimal("200000000"),
        maximum_economic_cost_currency=Decimal("200000000"),
        absolute_lambda=Decimal("0.5"),
        paired_lambda=Decimal("0.5"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )


def _cost_only_summary(*, precision: int, rounding: str):
    observation = ForwardDecisionObservation(
        sequence=0,
        universe_sha256=UNIVERSE_SHA,
        universe_event_sha256="1" * 64,
        challenger_decision_sha256="2" * 64,
        champion_decision_sha256="3" * 64,
    )
    exact_cost = Decimal("123456789.123456789")
    challenger = ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=0,
        universe_event_sha256=observation.universe_event_sha256,
        decision_sha256=observation.challenger_decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2),
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=exact_cost.copy_negate(),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
        economic_cost_currency=exact_cost,
        economic_cost_evidence_sha256=COST_SHA,
        economic_cost_available_at=T0 + timedelta(minutes=4),
    )
    champion = ResolvedPolicyOutcome(
        policy_id="champion",
        sequence=0,
        universe_event_sha256=observation.universe_event_sha256,
        decision_sha256=observation.champion_decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2),
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal(0),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
        economic_cost_currency=Decimal(0),
    )

    with localcontext() as context:
        context.prec = precision
        context.rounding = rounding
        accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
        accumulator.record(observation, _Resolver(challenger, champion))
        return accumulator.summary()


def test_causal_cost_delta_and_evidence_identity_ignore_ambient_context() -> None:
    low = _cost_only_summary(precision=5, rounding=ROUND_UP)
    high = _cost_only_summary(precision=50, rounding=ROUND_DOWN)

    expected = Decimal("123456789.123456789")
    assert low.challenger_total_pnl_currency == expected.copy_negate()
    assert low.challenger_max_drawdown_currency == expected
    assert low.evidence_sha256 == high.evidence_sha256
    assert low.challenger_total_pnl_currency == high.challenger_total_pnl_currency
    assert low.challenger_max_drawdown_currency == (
        high.challenger_max_drawdown_currency
    )
    assert low.positive_authority_verified is False
    assert low.conditional_eprocess_verified is False
    assert low.scientific_promotion_gate_passed is False
