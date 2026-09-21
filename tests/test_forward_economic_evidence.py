from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

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
T0 = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def registry(*, alpha: str = "0.2", total: str = "0.2") -> FamilywiseAlphaRegistry:
    return FamilywiseAlphaRegistry(
        family_id="forward-family-1",
        total_alpha=Decimal(total),
        allocations=(AlphaAllocation("challenger", Decimal(alpha)),),
        sealed_at=T0,
    )


def protocol(**overrides) -> ForwardEconomicProtocol:
    values = dict(
        protocol_id="forward-protocol-1",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="universe-1",
        universe_sha256=SHA_E,
        authority_binding_sha256=SHA_F,
        alpha_registry=registry(),
        minimum_events=5,
        risk_unit_currency=Decimal("10"),
        maximum_accepted_odds=Decimal("10"),
        maximum_drawdown_currency=Decimal("100"),
        absolute_lambda=Decimal("0.5"),
        paired_lambda=Decimal("0.5"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )
    values.update(overrides)
    return ForwardEconomicProtocol(**values)


def observation(seq: int) -> ForwardDecisionObservation:
    return ForwardDecisionObservation(
        sequence=seq,
        universe_sha256=SHA_E,
        universe_event_sha256=f"{seq % 16:x}" * 64,
        challenger_decision_sha256=SHA_A[:-1] + f"{seq % 16:x}",
        champion_decision_sha256=SHA_B[:-1] + f"{seq % 16:x}",
    )


def outcome(
    policy_id: str,
    obs: ForwardDecisionObservation,
    *,
    side: BetSide,
    pnl: str,
    odds: str | None = "2",
    stake: str | None = "10",
    decision_sha: str | None = None,
) -> ResolvedPolicyOutcome:
    if side is BetSide.NONE:
        return ResolvedPolicyOutcome(
            policy_id=policy_id,
            sequence=obs.sequence,
            universe_event_sha256=obs.universe_event_sha256,
            decision_sha256=decision_sha or (
                obs.challenger_decision_sha256 if policy_id == "challenger" else obs.champion_decision_sha256
            ),
            decision_committed_at=T0 + timedelta(minutes=2, seconds=obs.sequence),
            side=side,
            accepted_odds=None,
            accepted_stake=None,
            net_pnl_currency=Decimal(pnl),
            execution_evidence_sha256=None,
            execution_accepted_at=None,
            settlement_evidence_sha256=None,
            settlement_available_at=None,
        )
    return ResolvedPolicyOutcome(
        policy_id=policy_id,
        sequence=obs.sequence,
        universe_event_sha256=obs.universe_event_sha256,
        decision_sha256=decision_sha or (
            obs.challenger_decision_sha256 if policy_id == "challenger" else obs.champion_decision_sha256
        ),
        decision_committed_at=T0 + timedelta(minutes=2, seconds=obs.sequence),
        side=side,
        accepted_odds=Decimal(odds),
        accepted_stake=Decimal(stake),
        net_pnl_currency=Decimal(pnl),
        execution_evidence_sha256=SHA_C,
        execution_accepted_at=T0 + timedelta(minutes=3, seconds=obs.sequence),
        settlement_evidence_sha256=SHA_D,
        settlement_available_at=T0 + timedelta(hours=1, seconds=obs.sequence),
    )


class Resolver:
    def __init__(self, mapping, *, authority_sha256=SHA_F):
        self.mapping = mapping
        self.authority_sha256 = authority_sha256

    def resolve(self, *, policy_id, sequence, universe_event_sha256, decision_sha256):
        return self.mapping[(sequence, policy_id)]


def resolver_for(rows):
    mapping = {}
    for obs, challenger, champion in rows:
        mapping[(obs.sequence, "challenger")] = challenger
        mapping[(obs.sequence, "champion")] = champion
    return Resolver(mapping)


def test_alpha_registry_rejects_excess_familywise_allocation():
    with pytest.raises(ForwardEconomicEvidenceError, match="exceed"):
        FamilywiseAlphaRegistry(
            family_id="family",
            total_alpha=Decimal("0.05"),
            allocations=(AlphaAllocation("a", Decimal("0.03")), AlphaAllocation("b", Decimal("0.03"))),
            sealed_at=T0,
        )


def test_alpha_registry_requires_sorted_unique_challengers():
    with pytest.raises(ForwardEconomicEvidenceError, match="sorted"):
        FamilywiseAlphaRegistry(
            family_id="family",
            total_alpha=Decimal("0.1"),
            allocations=(AlphaAllocation("b", Decimal("0.02")), AlphaAllocation("a", Decimal("0.02"))),
            sealed_at=T0,
        )
    with pytest.raises(ForwardEconomicEvidenceError, match="unique"):
        FamilywiseAlphaRegistry(
            family_id="family",
            total_alpha=Decimal("0.1"),
            allocations=(AlphaAllocation("a", Decimal("0.02")), AlphaAllocation("a", Decimal("0.02"))),
            sealed_at=T0,
        )


def test_protocol_requires_preallocated_challenger():
    reg = FamilywiseAlphaRegistry(
        family_id="family",
        total_alpha=Decimal("0.1"),
        allocations=(AlphaAllocation("other", Decimal("0.05")),),
        sealed_at=T0,
    )
    with pytest.raises(ForwardEconomicEvidenceError, match="preallocated"):
        protocol(alpha_registry=reg)


def test_protocol_hash_binds_fixed_money_risk_unit():
    assert protocol(risk_unit_currency=Decimal("10")).identity_sha256 != protocol(
        risk_unit_currency=Decimal("20")
    ).identity_sha256


def test_protocol_hash_binds_universe_and_authority_commitments():
    base = protocol().identity_sha256
    assert protocol(universe_sha256=SHA_D).identity_sha256 != base
    assert protocol(authority_binding_sha256=SHA_C).identity_sha256 != base


def test_protocol_rejects_alpha_registry_sealed_after_freeze():
    reg = FamilywiseAlphaRegistry(
        family_id="family",
        total_alpha=Decimal("0.2"),
        allocations=(AlphaAllocation("challenger", Decimal("0.2")),),
        sealed_at=T0 + timedelta(hours=2),
    )
    with pytest.raises(ForwardEconomicEvidenceError, match="sealed"):
        protocol(alpha_registry=reg)


def test_gap_in_universe_sequence_fails_without_state_mutation():
    acc = ForwardEconomicEvidenceAccumulator(protocol())
    obs = observation(1)
    r = resolver_for([(obs, outcome("challenger", obs, side=BetSide.NONE, pnl="0"), outcome("champion", obs, side=BetSide.NONE, pnl="0"))])
    with pytest.raises(ForwardEconomicEvidenceError, match="contiguous"):
        acc.record(obs, r)
    assert acc.steps == ()
    assert acc.next_sequence == 0


def test_duplicate_universe_sequence_fails_without_state_mutation():
    acc = ForwardEconomicEvidenceAccumulator(protocol())
    obs = observation(0)
    r = resolver_for([(obs, outcome("challenger", obs, side=BetSide.NONE, pnl="0"), outcome("champion", obs, side=BetSide.NONE, pnl="0"))])
    acc.record(obs, r)
    before = acc.summary().evidence_sha256
    with pytest.raises(ForwardEconomicEvidenceError, match="contiguous"):
        acc.record(obs, r)
    assert len(acc.steps) == 1
    assert ac