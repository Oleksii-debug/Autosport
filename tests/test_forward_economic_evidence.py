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


def test_familywise_alpha_registry_accepts_exact_budget_boundary() -> None:
    allocation = Decimal("0.35")
    value = FamilywiseAlphaRegistry(
        family_id="forward-family-exact-boundary",
        total_alpha=Decimal("0.7"),
        allocations=(
            AlphaAllocation("challenger-a", allocation),
            AlphaAllocation("challenger-b", allocation),
        ),
        sealed_at=T0,
    )

    assert value.total_alpha == Decimal("0.7")
    assert tuple(item.alpha for item in value.allocations) == (allocation, allocation)


def test_familywise_alpha_registry_rejects_subcontext_overallocation() -> None:
    allocation = Decimal("0.35" + "0" * 79 + "6")

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="familywise alpha allocations exceed total_alpha",
    ):
        FamilywiseAlphaRegistry(
            family_id="forward-family-subcontext-overallocation",
            total_alpha=Decimal("0.7"),
            allocations=(
                AlphaAllocation("challenger-a", allocation),
                AlphaAllocation("challenger-b", allocation),
            ),
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
    assert acc.next_sequence == 1
    assert acc.summary().evidence_sha256 == before


def test_wrong_resolver_authority_fails_without_state_mutation():
    acc = ForwardEconomicEvidenceAccumulator(protocol())
    obs = observation(0)
    challenger = outcome("challenger", obs, side=BetSide.BACK, pnl="5")
    champion = outcome("champion", obs, side=BetSide.NONE, pnl="0")
    resolver = Resolver(
        {
            (0, "challenger"): challenger,
            (0, "champion"): champion,
        },
        authority_sha256=SHA_C,
    )
    before = acc.summary().evidence_sha256

    with pytest.raises(ForwardEconomicEvidenceError, match="frozen authority binding"):
        acc.record(obs, resolver)

    assert acc.steps == ()
    assert acc.next_sequence == 0
    assert acc.summary().evidence_sha256 == before


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("policy_id", "other", "policy identity"),
        ("sequence", 1, "sequence mismatch"),
        ("universe_event_sha256", SHA_C, "universe event mismatch"),
        ("decision_sha256", SHA_C, "decision digest mismatch"),
    ),
)
def test_resolved_challenger_identity_substitution_fails_atomically(
    field,
    value,
    match,
):
    acc = ForwardEconomicEvidenceAccumulator(protocol())
    obs = observation(0)
    challenger = outcome("challenger", obs, side=BetSide.BACK, pnl="5")
    challenger = replace(challenger, **{field: value})
    champion = outcome("champion", obs, side=BetSide.NONE, pnl="0")
    resolver = resolver_for([(obs, challenger, champion)])
    before = acc.summary().evidence_sha256

    with pytest.raises(ForwardEconomicEvidenceError, match=match):
        acc.record(obs, resolver)

    assert acc.steps == ()
    assert acc.summary().evidence_sha256 == before


def test_second_policy_identity_failure_does_not_commit_first_resolution():
    acc = ForwardEconomicEvidenceAccumulator(protocol())
    obs = observation(0)
    challenger = outcome("challenger", obs, side=BetSide.BACK, pnl="5")
    champion = replace(
        outcome("champion", obs, side=BetSide.NONE, pnl="0"),
        decision_sha256=SHA_C,
    )
    resolver = resolver_for([(obs, challenger, champion)])
    before = acc.summary().evidence_sha256

    with pytest.raises(ForwardEconomicEvidenceError, match="decision digest mismatch"):
        acc.record(obs, resolver)

    assert acc.steps == ()
    assert acc.next_sequence == 0
    assert acc.summary().evidence_sha256 == before


@pytest.mark.parametrize(
    "decision_committed_at",
    (
        T0,
        T0 + timedelta(minutes=1),
    ),
)
def test_decision_must_causally_follow_prospective_protocol_freeze(
    decision_committed_at,
):
    frozen_protocol = protocol()
    acc = ForwardEconomicEvidenceAccumulator(frozen_protocol)
    obs = observation(0)
    challenger = replace(
        outcome("challenger", obs, side=BetSide.BACK, pnl="5"),
        decision_committed_at=decision_committed_at,
    )
    champion = outcome("champion", obs, side=BetSide.NONE, pnl="0")
    resolver = resolver_for([(obs, challenger, champion)])

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="causally follow frozen prospective protocol",
    ):
        acc.record(obs, resolver)

    assert acc.steps == ()
    assert acc.next_sequence == 0


def test_protocol_maximum_accepted_odds_is_enforced_before_recording():
    acc = ForwardEconomicEvidenceAccumulator(
        protocol(maximum_accepted_odds=Decimal("2"))
    )
    obs = observation(0)
    challenger = outcome(
        "challenger",
        obs,
        side=BetSide.BACK,
        pnl="10",
        odds="3",
        stake="10",
    )
    champion = outcome("champion", obs, side=BetSide.NONE, pnl="0")

    with pytest.raises(ForwardEconomicEvidenceError, match="accepted odds exceed"):
        acc.record(obs, resolver_for([(obs, challenger, champion)]))

    assert acc.steps == ()


@pytest.mark.parametrize(
    ("side", "odds", "stake"),
    (
        (BetSide.BACK, "2", "11"),
        (BetSide.LAY, "3", "6"),
    ),
)
def test_fixed_risk_unit_rejects_excess_downside_exposure(side, odds, stake):
    acc = ForwardEconomicEvidenceAccumulator(protocol(risk_unit_currency=Decimal("10")))
    obs = observation(0)
    challenger = outcome(
        "challenger",
        obs,
        side=side,
        pnl="0",
        odds=odds,
        stake=stake,
    )
    champion = outcome("champion", obs, side=BetSide.NONE, pnl="0")

    with pytest.raises(ForwardEconomicEvidenceError, match="fixed risk unit"):
        acc.record(obs, resolver_for([(obs, challenger, champion)]))

    assert acc.steps == ()


def test_none_outcome_cannot_carry_money_or_execution_claims():
    obs = observation(0)
    none = outcome("challenger", obs, side=BetSide.NONE, pnl="0")

    with pytest.raises(ForwardEconomicEvidenceError, match="accepted odds or stake"):
        replace(
            none,
            accepted_odds=Decimal("2"),
            accepted_stake=Decimal("10"),
        )

    with pytest.raises(ForwardEconomicEvidenceError, match="exactly zero money"):
        replace(none, net_pnl_currency=Decimal("1"))

    with pytest.raises(ForwardEconomicEvidenceError, match="execution or settlement"):
        replace(
            none,
            execution_evidence_sha256=SHA_C,
            settlement_evidence_sha256=SHA_D,
        )


def test_successful_record_binds_exact_money_and_evidence_identity():
    acc = ForwardEconomicEvidenceAccumulator(protocol())
    obs = observation(0)
    challenger = outcome("challenger", obs, side=BetSide.BACK, pnl="5")
    champion = outcome("champion", obs, side=BetSide.NONE, pnl="0")

    step = acc.record(obs, resolver_for([(obs, challenger, champion)]))
    summary = acc.summary()

    assert step.sequence == 0
    assert step.challenger_net_pnl_currency == Decimal("5")
    assert step.challenger_normalized_pnl == Decimal("0.5")
    assert step.paired_normalized_pnl == Decimal("0.5")
    assert step.challenger_execution_evidence_sha256 == SHA_C
    assert step.challenger_settlement_evidence_sha256 == SHA_D
    assert summary.observed_events == 1
    assert summary.next_sequence == 1
    assert summary.challenger_total_pnl_currency == Decimal("5")
    assert summary.champion_total_pnl_currency == Decimal("0")
    assert summary.positive_authority_verified is False
    assert summary.scientific_promotion_gate_passed is False
    assert summary.promotion_authority is False


def test_nonzero_start_sequence_is_part_of_prospective_contract():
    acc = ForwardEconomicEvidenceAccumulator(protocol(start_sequence=5))
    assert acc.next_sequence == 5

    too_early = observation(0)
    with pytest.raises(ForwardEconomicEvidenceError, match="contiguous"):
        acc.record(
            too_early,
            resolver_for(
                [
                    (
                        too_early,
                        outcome("challenger", too_early, side=BetSide.NONE, pnl="0"),
                        outcome("champion", too_early, side=BetSide.NONE, pnl="0"),
                    )
                ]
            ),
        )

    obs = observation(5)
    acc.record(
        obs,
        resolver_for(
            [
                (
                    obs,
                    outcome("challenger", obs, side=BetSide.NONE, pnl="0"),
                    outcome("champion", obs, side=BetSide.NONE, pnl="0"),
                )
            ]
        ),
    )
    assert acc.next_sequence == 6


def test_statistical_thresholds_can_cross_but_caller_resolver_stays_non_promoting():
    acc = ForwardEconomicEvidenceAccumulator(protocol())

    rows = []
    for seq in range(5):
        obs = observation(seq)
        rows.append(
            (
                obs,
                outcome("challenger", obs, side=BetSide.BACK, pnl="10"),
                outcome("champion", obs, side=BetSide.NONE, pnl="0"),
            )
        )
    resolver = resolver_for(rows)
    for obs, _, _ in rows:
        acc.record(obs, resolver)

    summary = acc.summary()
    with localcontext() as context:
        context.prec = 80
        expected_increment = (
            Decimal("0.5") * Decimal("1")
            - (
                Decimal("0.5")
                * Decimal("0.5")
                * Decimal("2")
                * Decimal("2")
            )
            / Decimal("8")
        )
        expected_log_e = +(Decimal(5) * expected_increment)

    assert summary.absolute_log_e == expected_log_e
    assert summary.paired_log_e == expected_log_e
    assert summary.minimum_events_satisfied is True
    assert summary.absolute_threshold_crossed is True
    assert summary.paired_threshold_crossed is True
    assert summary.drawdown_guard_passed is True

    # Numerical threshold crossing is not optional-stopping authority. Until
    # product-owned evidence proves the frozen filtration/conditional-null
    # contract and the REAL economic resolver exists, this remains diagnostic.
    assert summary.conditional_eprocess_verified is False
    assert summary.positive_authority_verified is False
    assert summary.scientific_promotion_gate_passed is False
    assert summary.promotion_authority is False


def test_summary_payload_preserves_explicit_fail_closed_authority_truth():
    summary = ForwardEconomicEvidenceAccumulator(protocol()).summary()
    payload = summary.to_payload()

    assert payload["positive_authority_verified"] is False
    assert payload["conditional_eprocess_verified"] is False
    assert payload["scientific_promotion_gate_passed"] is False
    assert payload["promotion_authority"] is False
    assert payload["protocol_sha256"] == protocol().identity_sha256
    assert payload["observed_events"] == 0
    assert payload["next_sequence"] == 0

def test_all_in_protocol_binds_frozen_economic_cost_support():
    base = protocol(maximum_economic_cost_currency=Decimal("0"))
    bounded = protocol(maximum_economic_cost_currency=Decimal("2"))
    assert base.identity_sha256 != bounded.identity_sha256
    assert bounded.maximum_economic_cost_currency == Decimal("2")
    with pytest.raises(ForwardEconomicEvidenceError, match="must be non-negative"):
        protocol(maximum_economic_cost_currency=Decimal("-0.01"))


def test_none_preserves_authoritative_nonzero_cost_and_causal_availability():
    acc = ForwardEconomicEvidenceAccumulator(
        protocol(
            minimum_events=1,
            maximum_economic_cost_currency=Decimal("2"),
            maximum_drawdown_currency=Decimal("0.5"),
        )
    )
    obs = observation(0)
    cost_available_at = T0 + timedelta(hours=2)
    challenger = ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=obs.sequence,
        universe_event_sha256=obs.universe_event_sha256,
        decision_sha256=obs.challenger_decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2),
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal("-1"),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
        economic_cost_currency=Decimal("1"),
        economic_cost_evidence_sha256=SHA_C,
        economic_cost_incurred_at=T0 + timedelta(minutes=2),
        economic_cost_available_at=cost_available_at,
    )
    champion = outcome("champion", obs, side=BetSide.NONE, pnl="0")

    step = acc.record(obs, resolver_for([(obs, challenger, champion)]))
    summary = acc.summary()

    assert step.challenger_wager_pnl_currency == Decimal("0")
    assert step.challenger_economic_cost_currency == Decimal("1")
    assert step.challenger_economic_available_at == cost_available_at
    assert step.challenger_normalized_pnl == Decimal("-0.1")
    assert step.absolute_low == Decimal("-0.2")
    assert step.absolute_high == Decimal("0")
    assert summary.challenger_total_pnl_currency == Decimal("-1")
    assert summary.challenger_max_drawdown_currency == Decimal("1")
    assert summary.drawdown_guard_passed is False
    assert summary.positive_authority_verified is False
    assert summary.scientific_promotion_gate_passed is False


def test_back_all_in_loss_can_extend_below_gross_wager_bound_when_cost_is_bounded():
    acc = ForwardEconomicEvidenceAccumulator(
        protocol(
            minimum_events=1,
            maximum_economic_cost_currency=Decimal("2"),
        )
    )
    obs = observation(0)
    challenger = ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=obs.sequence,
        universe_event_sha256=obs.universe_event_sha256,
        decision_sha256=obs.challenger_decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2),
        side=BetSide.BACK,
        accepted_odds=Decimal("2"),
        accepted_stake=Decimal("10"),
        net_pnl_currency=Decimal("-11"),
        execution_evidence_sha256=SHA_C,
        execution_accepted_at=T0 + timedelta(minutes=3),
        settlement_evidence_sha256=SHA_D,
        settlement_available_at=T0 + timedelta(hours=1),
        wager_pnl_currency=Decimal("-10"),
        economic_cost_currency=Decimal("1"),
        economic_cost_evidence_sha256=SHA_A,
        economic_cost_incurred_at=T0 + timedelta(minutes=2),
        economic_cost_available_at=T0 + timedelta(hours=2),
    )
    champion = outcome("champion", obs, side=BetSide.NONE, pnl="0")

    step = acc.record(obs, resolver_for([(obs, challenger, champion)]))

    assert step.challenger_wager_pnl_currency == Decimal("-10")
    assert step.challenger_net_pnl_currency == Decimal("-11")
    assert step.challenger_normalized_pnl == Decimal("-1.1")
    assert step.absolute_low == Decimal("-1.2")
    assert step.absolute_high == Decimal("1")
    assert step.challenger_economic_available_at == T0 + timedelta(hours=2)
    assert acc.summary().challenger_max_drawdown_currency == Decimal("11")


def test_nonzero_economic_cost_requires_bound_evidence():
    obs = observation(0)
    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="requires canonical evidence and availability",
    ):
        ResolvedPolicyOutcome(
            policy_id="challenger",
            sequence=obs.sequence,
            universe_event_sha256=obs.universe_event_sha256,
            decision_sha256=obs.challenger_decision_sha256,
            decision_committed_at=T0 + timedelta(minutes=2),
            side=BetSide.NONE,
            accepted_odds=None,
            accepted_stake=None,
            net_pnl_currency=Decimal("-1"),
            execution_evidence_sha256=None,
            execution_accepted_at=None,
            settlement_evidence_sha256=None,
            settlement_available_at=None,
            economic_cost_currency=Decimal("1"),
        )


def test_cost_above_frozen_support_fails_without_accumulator_mutation():
    acc = ForwardEconomicEvidenceAccumulator(
        protocol(maximum_economic_cost_currency=Decimal("1"))
    )
    obs = observation(0)
    challenger = ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=obs.sequence,
        universe_event_sha256=obs.universe_event_sha256,
        decision_sha256=obs.challenger_decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2),
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal("-2"),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
        economic_cost_currency=Decimal("2"),
        economic_cost_evidence_sha256=SHA_C,
        economic_cost_incurred_at=T0 + timedelta(minutes=2),
        economic_cost_available_at=T0 + timedelta(hours=1),
    )
    champion = outcome("champion", obs, side=BetSide.NONE, pnl="0")
    resolver = resolver_for([(obs, challenger, champion)])

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="exceeds frozen protocol maximum",
    ):
        acc.record(obs, resolver)
    assert acc.steps == ()
    assert acc.next_sequence == 0


def test_cost_authority_and_availability_are_bound_into_evidence_identity():
    obs = observation(0)
    champion = outcome("champion", obs, side=BetSide.NONE, pnl="0")

    def evidence(cost_sha: str, cost_time: datetime) -> str:
        challenger = ResolvedPolicyOutcome(
            policy_id="challenger",
            sequence=obs.sequence,
            universe_event_sha256=obs.universe_event_sha256,
            decision_sha256=obs.challenger_decision_sha256,
            decision_committed_at=T0 + timedelta(minutes=2),
            side=BetSide.NONE,
            accepted_odds=None,
            accepted_stake=None,
            net_pnl_currency=Decimal("-1"),
            execution_evidence_sha256=None,
            execution_accepted_at=None,
            settlement_evidence_sha256=None,
            settlement_available_at=None,
            economic_cost_currency=Decimal("1"),
            economic_cost_evidence_sha256=cost_sha,
            economic_cost_incurred_at=T0 + timedelta(minutes=2),
            economic_cost_available_at=cost_time,
        )
        acc = ForwardEconomicEvidenceAccumulator(
            protocol(maximum_economic_cost_currency=Decimal("2"))
        )
        acc.record(obs, resolver_for([(obs, challenger, champion)]))
        return acc.summary().evidence_sha256

    base = evidence(SHA_C, T0 + timedelta(hours=1))
    assert evidence(SHA_D, T0 + timedelta(hours=1)) != base
    assert evidence(SHA_C, T0 + timedelta(hours=2)) != base

def test_early_cost_debit_contributes_to_drawdown_before_later_wager_profit():
    acc = ForwardEconomicEvidenceAccumulator(
        protocol(
            minimum_events=1,
            maximum_economic_cost_currency=Decimal("2"),
            maximum_drawdown_currency=Decimal("0.5"),
        )
    )
    obs = observation(0)
    challenger = ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=obs.sequence,
        universe_event_sha256=obs.universe_event_sha256,
        decision_sha256=obs.challenger_decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2),
        side=BetSide.BACK,
        accepted_odds=Decimal("2"),
        accepted_stake=Decimal("10"),
        net_pnl_currency=Decimal("9"),
        execution_evidence_sha256=SHA_C,
        execution_accepted_at=T0 + timedelta(minutes=3),
        settlement_evidence_sha256=SHA_D,
        settlement_available_at=T0 + timedelta(hours=1),
        wager_pnl_currency=Decimal("10"),
        economic_cost_currency=Decimal("1"),
        economic_cost_evidence_sha256=SHA_A,
        economic_cost_incurred_at=T0 + timedelta(minutes=2),
        economic_cost_available_at=T0 + timedelta(minutes=30),
    )
    champion = outcome("champion", obs, side=BetSide.NONE, pnl="0")

    acc.record(obs, resolver_for([(obs, challenger, champion)]))
    summary = acc.summary()

    assert summary.challenger_total_pnl_currency == Decimal("9")
    assert summary.challenger_peak_pnl_currency == Decimal("9")
    assert summary.challenger_max_drawdown_currency == Decimal("1")
    assert summary.drawdown_guard_passed is False
    assert summary.positive_authority_verified is False

def test_executed_cost_requires_explicit_gross_wager_pnl():
    obs = observation(0)
    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="requires explicit wager P&L",
    ):
        ResolvedPolicyOutcome(
            policy_id="challenger",
            sequence=obs.sequence,
            universe_event_sha256=obs.universe_event_sha256,
            decision_sha256=obs.challenger_decision_sha256,
            decision_committed_at=T0 + timedelta(minutes=2),
            side=BetSide.BACK,
            accepted_odds=Decimal("2"),
            accepted_stake=Decimal("10"),
            net_pnl_currency=Decimal("-11"),
            execution_evidence_sha256=SHA_C,
            execution_accepted_at=T0 + timedelta(minutes=3),
            settlement_evidence_sha256=SHA_D,
            settlement_available_at=T0 + timedelta(hours=1),
            economic_cost_currency=Decimal("1"),
            economic_cost_evidence_sha256=SHA_A,
            economic_cost_incurred_at=T0 + timedelta(minutes=2),
            economic_cost_available_at=T0 + timedelta(hours=2),
        )

def test_zero_cost_requires_explicit_evidence_for_all_in_completeness():
    obs = observation(0)
    legacy_acc = ForwardEconomicEvidenceAccumulator(protocol(minimum_events=1))
    legacy_acc.record(
        obs,
        resolver_for(
            [
                (
                    obs,
                    outcome("challenger", obs, side=BetSide.NONE, pnl="0"),
                    outcome("champion", obs, side=BetSide.NONE, pnl="0"),
                )
            ]
        ),
    )
    assert legacy_acc.summary().all_in_economics_complete is False

    cost_available_at = T0 + timedelta(minutes=30)
    challenger = ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=obs.sequence,
        universe_event_sha256=obs.universe_event_sha256,
        decision_sha256=obs.challenger_decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2),
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal("0"),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
        economic_cost_currency=Decimal("0"),
        economic_cost_evidence_sha256=SHA_C,
        economic_cost_available_at=cost_available_at,
    )
    champion = ResolvedPolicyOutcome(
        policy_id="champion",
        sequence=obs.sequence,
        universe_event_sha256=obs.universe_event_sha256,
        decision_sha256=obs.champion_decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2),
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal("0"),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
        economic_cost_currency=Decimal("0"),
        economic_cost_evidence_sha256=SHA_D,
        economic_cost_available_at=cost_available_at,
    )
    complete_acc = ForwardEconomicEvidenceAccumulator(protocol(minimum_events=1))
    complete_acc.record(obs, resolver_for([(obs, challenger, champion)]))
    complete = complete_acc.summary()

    assert complete.all_in_economics_complete is True
    assert complete.positive_authority_verified is False
    assert complete.scientific_promotion_gate_passed is False

