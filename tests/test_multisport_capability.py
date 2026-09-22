from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.multisport_capability import (
    CapabilityRecommendationStatus,
    MultiSportCapabilityError,
    SportCapabilityDecision,
    SportCapabilityTarget,
    project_multisport_capability,
)
from autosport.sport_domain_fitness import (
    DomainProfile,
    EvidenceProvenance,
    EvidenceState,
    MetricEvidence,
    SportDomainFitnessObservation,
)


T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T1_PLUS_2 = "2026-01-02T00:00:02Z"
T1_PLUS_5 = "2026-01-02T00:00:05Z"
T1_PLUS_11 = "2026-01-02T00:00:11Z"
T2 = "2026-01-03T00:00:00Z"


def met(value, unit, state=EvidenceState.MEASURED):
    return MetricEvidence(
        state,
        None if value is None else Decimal(str(value)),
        unit,
    )


def make_observation(**overrides):
    values = dict(
        observation_id="obs-default",
        sport_id="football",
        league_id="league-1",
        market_id="match",
        provider_id="provider-1",
        measured_from=T0,
        measured_until=T1,
        available_at=T1,
        evidence_sha256="a" * 64,
        provenance=EvidenceProvenance.OBSERVED,
        domain_profile=DomainProfile.FAST,
        catalogue_coverage=met("0.9", "fraction"),
        quote_coverage=met("0.8", "fraction"),
        recurrence_per_hour=met("12", "events/hour"),
        freshness_seconds=met("1", "seconds"),
        reaction_slack_seconds=met("5", "seconds"),
        executable_liquidity=met("100", "units"),
        fee_fraction=met("0.01", "fraction"),
        slippage_fraction=met("0.01", "fraction"),
        capital_time_hours=met("0.25", "hours"),
        data_cost=met("0.10", "cost"),
        compute_cost=met("2", "cost"),
        compute_duration_seconds=met("2", "seconds"),
        slow_analysis_deadline_seconds=met("1", "seconds"),
        freshness_ttl_seconds=met("10", "seconds"),
    )
    values.update(overrides)
    return SportDomainFitnessObservation(**values)


def target(
    sport_id="football",
    league_id="league-1",
    market_id="match",
    provider_id="provider-1",
):
    return SportCapabilityTarget(
        sport_id=sport_id,
        league_id=league_id,
        market_id=market_id,
        provider_id=provider_id,
    )


def by_sport(snapshot):
    return {item.target.sport_id: item for item in snapshot.decisions}


def test_two_sports_are_projected_independently():
    football = make_observation(observation_id="football")
    tennis = make_observation(
        observation_id="tennis",
        sport_id="tennis",
        league_id="atp",
        domain_profile=DomainProfile.SLOW,
        slow_analysis_deadline_seconds=met("3", "seconds"),
        evidence_sha256="b" * 64,
    )

    snapshot = project_multisport_capability(
        [target(), target("tennis", "atp")],
        [tennis, football],
        as_of=T1_PLUS_5,
    )

    decisions = by_sport(snapshot)
    assert (
        decisions["football"].status
        is CapabilityRecommendationStatus.RECOMMEND_BASELINE
    )
    assert (
        decisions["tennis"].status
        is CapabilityRecommendationStatus.RECOMMEND_SLOW_RESEARCH
    )
    assert {item.sport_id for item in snapshot.recommended_targets} == {
        "football",
        "tennis",
    }


def test_simulated_one_sport_blocks_only_that_exact_target():
    football = make_observation(observation_id="football")
    tennis = make_observation(
        observation_id="tennis-sim",
        sport_id="tennis",
        league_id="atp",
        evidence_sha256="b" * 64,
        provenance=EvidenceProvenance.SIMULATED,
    )

    snapshot = project_multisport_capability(
        [target(), target("tennis", "atp")],
        [football, tennis],
        as_of=T1_PLUS_5,
    )

    decisions = by_sport(snapshot)
    assert decisions["football"].recommended is True
    assert (
        decisions["tennis"].status
        is CapabilityRecommendationStatus.BLOCKED
    )
    assert decisions["tennis"].observation_id == "tennis-sim"


def test_future_evidence_is_not_disclosed_as_recommendation_evidence():
    future = make_observation(
        observation_id="future",
        measured_until=T2,
        available_at=T2,
    )

    decision = project_multisport_capability(
        [target()],
        [future],
        as_of=T1_PLUS_5,
    ).decisions[0]

    assert (
        decision.status
        is CapabilityRecommendationStatus.INSUFFICIENT_EVIDENCE
    )
    assert decision.observation_id is None
    assert decision.evidence_sha256 is None


def test_latest_causal_observation_controls_projection():
    older = make_observation(
        observation_id="older-good",
        evidence_sha256="a" * 64,
    )
    newer = make_observation(
        observation_id="newer-missing",
        available_at=T1_PLUS_2,
        evidence_sha256="b" * 64,
        executable_liquidity=MetricEvidence(
            EvidenceState.UNKNOWN,
            None,
            "units",
        ),
    )

    decision = project_multisport_capability(
        [target()],
        [older, newer],
        as_of=T1_PLUS_5,
    ).decisions[0]

    assert (
        decision.status
        is CapabilityRecommendationStatus.INSUFFICIENT_EVIDENCE
    )
    assert decision.observation_id == "newer-missing"
    assert decision.evidence_sha256 == "b" * 64


def test_equally_current_evidence_blocks_iteration_order_winner():
    first = make_observation(
        observation_id="same-a",
        evidence_sha256="a" * 64,
    )
    second = make_observation(
        observation_id="same-b",
        evidence_sha256="b" * 64,
    )

    decision = project_multisport_capability(
        [target()],
        [second, first],
        as_of=T1_PLUS_5,
    ).decisions[0]

    assert decision.status is CapabilityRecommendationStatus.BLOCKED
    assert decision.observation_id is None
    assert decision.evidence_sha256 is None
    assert "ambiguous" in decision.reason


def test_stale_latest_evidence_blocks_without_falling_back():
    older = make_observation(
        observation_id="old-good",
        available_at=T1,
        evidence_sha256="a" * 64,
    )
    latest = make_observation(
        observation_id="latest-stale",
        available_at=T1_PLUS_2,
        evidence_sha256="b" * 64,
        freshness_seconds=met("10", "seconds"),
        freshness_ttl_seconds=met("10", "seconds"),
    )

    decision = project_multisport_capability(
        [target()],
        [older, latest],
        as_of=T1_PLUS_11,
    ).decisions[0]

    assert decision.status is CapabilityRecommendationStatus.BLOCKED
    assert decision.observation_id == "latest-stale"


@pytest.mark.parametrize(
    ("override", "requested"),
    [
        ({"provider_id": "provider-2"}, target()),
        ({"market_id": "totals"}, target()),
        ({"league_id": "league-2"}, target()),
        (
            {"sport_id": "tennis"},
            target("football", "league-1"),
        ),
    ],
)
def test_exact_target_identity_isolation(override, requested):
    observation = make_observation(
        observation_id="foreign",
        **override,
    )

    decision = project_multisport_capability(
        [requested],
        [observation],
        as_of=T1_PLUS_5,
    ).decisions[0]

    assert (
        decision.status
        is CapabilityRecommendationStatus.INSUFFICIENT_EVIDENCE
    )
    assert decision.observation_id is None


def test_snapshot_is_deterministic_under_input_order_and_time_spelling():
    football = make_observation(observation_id="football")
    tennis = make_observation(
        observation_id="tennis",
        sport_id="tennis",
        league_id="atp",
        evidence_sha256="b" * 64,
    )
    targets = [target(), target("tennis", "atp")]

    first = project_multisport_capability(
        targets,
        [football, tennis],
        as_of=T1_PLUS_5,
    )
    second = project_multisport_capability(
        list(reversed(targets)),
        [tennis, football],
        as_of="2026-01-01T19:00:05-05:00",
    )

    assert first == second
    assert first.snapshot_id == second.snapshot_id
    assert first.to_dict() == second.to_dict()


def test_duplicate_targets_and_bounded_inputs_fail_closed():
    with pytest.raises(
        MultiSportCapabilityError,
        match="duplicates",
    ):
        project_multisport_capability(
            [target(), target()],
            [],
            as_of=T1_PLUS_5,
        )

    with pytest.raises(
        MultiSportCapabilityError,
        match="target count",
    ):
        project_multisport_capability(
            [target(league_id=f"league-{index}") for index in range(257)],
            [],
            as_of=T1_PLUS_5,
        )

    with pytest.raises(
        MultiSportCapabilityError,
        match="observation count",
    ):
        project_multisport_capability(
            [target()],
            [make_observation(observation_id=f"obs-{index}") for index in range(4097)],
            as_of=T1_PLUS_5,
        )



def test_caller_constructed_favorable_observation_stays_recommendation_only():
    snapshot = project_multisport_capability(
        [target()],
        [make_observation(observation_id="caller-favorable")],
        as_of=T1_PLUS_5,
    )
    decision = snapshot.decisions[0]

    assert (
        decision.status
        is CapabilityRecommendationStatus.RECOMMEND_BASELINE
    )
    assert decision.recommended is True
    assert not hasattr(decision, "admitted")
    assert not hasattr(snapshot, "admitted_targets")
    assert all(
        "ADMIT" not in status.value
        for status in CapabilityRecommendationStatus
    )

def test_projection_contract_cannot_expand_into_money_or_execution_authority():
    decision = project_multisport_capability(
        [target()],
        [make_observation()],
        as_of=T1_PLUS_5,
    ).decisions[0]

    assert set(SportCapabilityDecision.__dataclass_fields__) == {
        "target",
        "status",
        "observation_id",
        "evidence_sha256",
        "reason",
    }
    assert not hasattr(decision, "stake")
    assert not hasattr(decision, "execution_authorized")
    assert not hasattr(decision, "risk_policy")
    assert not hasattr(decision, "provider_write")
