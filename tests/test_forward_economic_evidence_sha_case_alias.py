from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

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
T0 = datetime(2026, 9, 22, 21, 0, tzinfo=UTC)
UNIVERSE_SHA = "e" * 64
AUTHORITY_SHA = "f" * 64
CANONICAL_EVENT_SHA = "a" * 64


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="forward-family-sha-case-alias",
        total_alpha=Decimal("0.2"),
        allocations=(AlphaAllocation("challenger", Decimal("0.2")),),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="forward-protocol-sha-case-alias",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="universe-sha-case-alias",
        universe_sha256=UNIVERSE_SHA,
        authority_binding_sha256=AUTHORITY_SHA,
        alpha_registry=registry,
        minimum_events=2,
        risk_unit_currency=Decimal("10"),
        maximum_accepted_odds=Decimal("10"),
        maximum_drawdown_currency=Decimal("100"),
        absolute_lambda=Decimal("0.5"),
        paired_lambda=Decimal("0.5"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )


def _observation(
    sequence: int,
    event_sha256: str,
    challenger_decision_sha256: str,
    champion_decision_sha256: str,
) -> ForwardDecisionObservation:
    return ForwardDecisionObservation(
        sequence=sequence,
        universe_sha256=UNIVERSE_SHA,
        universe_event_sha256=event_sha256,
        challenger_decision_sha256=challenger_decision_sha256,
        champion_decision_sha256=champion_decision_sha256,
    )


def _none_outcome(
    policy_id: str,
    observation: ForwardDecisionObservation,
) -> ResolvedPolicyOutcome:
    decision_sha256 = (
        observation.challenger_decision_sha256
        if policy_id == "challenger"
        else observation.champion_decision_sha256
    )
    return ResolvedPolicyOutcome(
        policy_id=policy_id,
        sequence=observation.sequence,
        universe_event_sha256=observation.universe_event_sha256,
        decision_sha256=decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2, seconds=observation.sequence),
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal("0"),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
    )


class _Resolver:
    def __init__(self, observation: ForwardDecisionObservation) -> None:
        self.authority_sha256 = AUTHORITY_SHA
        self._outcomes = {
            "challenger": _none_outcome("challenger", observation),
            "champion": _none_outcome("champion", observation),
        }
        self.calls: list[str] = []

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        self.calls.append(policy_id)
        return self._outcomes[policy_id]


def test_sha256_case_alias_cannot_mint_second_universe_member() -> None:
    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    first = _observation(0, CANONICAL_EVENT_SHA, "1" * 64, "2" * 64)
    aliased_duplicate = _observation(
        1,
        CANONICAL_EVENT_SHA.upper(),
        "3" * 64,
        "4" * 64,
    )

    first_step = accumulator.record(first, _Resolver(first))
    before = accumulator.summary()
    duplicate_resolver = _Resolver(aliased_duplicate)

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="cannot be counted more than once",
    ):
        accumulator.record(aliased_duplicate, duplicate_resolver)

    after = accumulator.summary()
    assert duplicate_resolver.calls == []
    assert accumulator.steps == (first_step,)
    assert after.observed_events == 1
    assert after.next_sequence == 1
    assert after.evidence_sha256 == before.evidence_sha256
    assert after.positive_authority_verified is False
    assert after.conditional_eprocess_verified is False
    assert after.scientific_promotion_gate_passed is False
