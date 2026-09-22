from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from autosport.live_market_abstention import (
    AbstentionReason,
    ContinuityStatus,
    LiveMarketAbstentionError,
    LiveMarketEligibility,
    LiveMarketEligibilityDecision,
    LiveMarketEligibilityInput,
    MarketStatus,
    ProviderHealth,
    evaluate_live_market_eligibility,
)


NOW = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)


def clean() -> LiveMarketEligibilityInput:
    return LiveMarketEligibilityInput(
        quote_observed_at=NOW - timedelta(seconds=1),
        decision_observed_at=NOW,
        max_quote_age=timedelta(seconds=5),
        market_status=MarketStatus.OPEN,
        market_data_delayed=False,
        continuity_status=ContinuityStatus.COHERENT,
        response_coverage_complete=True,
        continuity_epoch="epoch-7",
        expected_continuity_epoch="epoch-7",
        provider_health=ProviderHealth.HEALTHY,
        source_actionability_proven=True,
    )


def reason(evidence: LiveMarketEligibilityInput, expected: AbstentionReason) -> None:
    decision = evaluate_live_market_eligibility(evidence)
    assert decision.status is LiveMarketEligibility.WAIT
    assert expected in decision.reasons
    assert decision.execution_authorized is False
    assert decision.eligible_for_downstream_evaluation is False


def test_clean_snapshot_is_only_eligible_for_downstream_evaluation() -> None:
    result = evaluate_live_market_eligibility(clean())
    assert result.status is LiveMarketEligibility.ELIGIBLE_FOR_DOWNSTREAM_EVALUATION
    assert result.reasons == ()
    assert result.quote_age == timedelta(seconds=1)
    assert result.execution_authorized is False
    assert result.eligible_for_downstream_evaluation is True


def test_decision_authority_flags_are_hard_false_properties() -> None:
    result = evaluate_live_market_eligibility(clean())

    assert result.execution_authorized is False
    assert result.provider_authorities_bound is False
    with pytest.raises(TypeError):
        LiveMarketEligibilityDecision(
            status=LiveMarketEligibility.ELIGIBLE_FOR_DOWNSTREAM_EVALUATION,
            reasons=(),
            quote_age=timedelta(seconds=1),
            execution_authorized=True,  # type: ignore[call-arg]
        )


@pytest.mark.parametrize(
    ("status", "reasons"),
    [
        (LiveMarketEligibility.WAIT, ()),
        (
            LiveMarketEligibility.ELIGIBLE_FOR_DOWNSTREAM_EVALUATION,
            (AbstentionReason.STALE_QUOTE,),
        ),
    ],
)
def test_manually_constructed_decision_state_must_be_consistent(
    status: LiveMarketEligibility,
    reasons: tuple[AbstentionReason, ...],
) -> None:
    with pytest.raises(LiveMarketAbstentionError):
        LiveMarketEligibilityDecision(
            status=status,
            reasons=reasons,
            quote_age=timedelta(seconds=1),
        )


def test_manually_constructed_decision_shape_fails_closed() -> None:
    with pytest.raises(LiveMarketAbstentionError):
        LiveMarketEligibilityDecision(
            status=LiveMarketEligibility.WAIT,
            reasons=(object(),),  # type: ignore[arg-type]
            quote_age=timedelta(seconds=1),
        )


def test_exact_freshness_boundary_is_conservative_wait() -> None:
    reason(
        replace(clean(), quote_observed_at=NOW - timedelta(seconds=5)),
        AbstentionReason.STALE_QUOTE,
    )


def test_one_microsecond_inside_boundary_is_eligible() -> None:
    result = evaluate_live_market_eligibility(
        replace(clean(), quote_observed_at=NOW - timedelta(seconds=4, microseconds=999999))
    )
    assert result.eligible_for_downstream_evaluation


def test_future_quote_waits() -> None:
    reason(
        replace(clean(), quote_observed_at=NOW + timedelta(microseconds=1)),
        AbstentionReason.FUTURE_QUOTE,
    )


@pytest.mark.parametrize(
    "status",
    [MarketStatus.SUSPENDED, MarketStatus.CLOSED, MarketStatus.INACTIVE, MarketStatus.UNKNOWN],
)
def test_non_open_market_waits(status: MarketStatus) -> None:
    reason(replace(clean(), market_status=status), AbstentionReason.MARKET_NOT_OPEN)


@pytest.mark.parametrize("delayed", [True, None])
def test_delayed_or_unknown_provider_classification_waits(delayed: bool | None) -> None:
    reason(
        replace(clean(), market_data_delayed=delayed),
        AbstentionReason.MARKET_DATA_DELAYED_OR_UNKNOWN,
    )


@pytest.mark.parametrize(
    "continuity",
    [ContinuityStatus.GAP, ContinuityStatus.RECONCILING, ContinuityStatus.UNKNOWN],
)
def test_noncoherent_continuity_waits(continuity: ContinuityStatus) -> None:
    reason(
        replace(clean(), continuity_status=continuity),
        AbstentionReason.CONTINUITY_NOT_COHERENT,
    )


def test_incomplete_response_coverage_waits() -> None:
    reason(
        replace(clean(), response_coverage_complete=False),
        AbstentionReason.RESPONSE_COVERAGE_INCOMPLETE,
    )


def test_wrong_continuity_epoch_waits() -> None:
    reason(
        replace(clean(), continuity_epoch="epoch-6"),
        AbstentionReason.CONTINUITY_EPOCH_MISMATCH,
    )


@pytest.mark.parametrize("health", [ProviderHealth.DEGRADED, ProviderHealth.UNKNOWN])
def test_provider_not_healthy_waits(health: ProviderHealth) -> None:
    reason(
        replace(clean(), provider_health=health),
        AbstentionReason.PROVIDER_HEALTH_NOT_HEALTHY,
    )


def test_unproven_source_actionability_waits() -> None:
    reason(
        replace(clean(), source_actionability_proven=False),
        AbstentionReason.SOURCE_ACTIONABILITY_UNPROVEN,
    )


def test_explicit_ambiguity_waits() -> None:
    reason(
        replace(clean(), explicit_ambiguity=True),
        AbstentionReason.EXPLICIT_AMBIGUITY,
    )


def test_conflation_only_blocks_strategies_that_require_intermediate_microstates() -> None:
    coarse = evaluate_live_market_eligibility(replace(clean(), stream_conflated=True))
    assert coarse.eligible_for_downstream_evaluation
    reason(
        replace(
            clean(),
            stream_conflated=True,
            requires_intermediate_granularity=True,
        ),
        AbstentionReason.INSUFFICIENT_INTERMEDIATE_GRANULARITY,
    )


def test_intermediate_granularity_requirement_without_conflation_is_not_itself_a_blocker() -> None:
    result = evaluate_live_market_eligibility(
        replace(clean(), requires_intermediate_granularity=True)
    )
    assert result.eligible_for_downstream_evaluation


def test_multiple_failures_are_preserved_not_collapsed_to_one_reason() -> None:
    result = evaluate_live_market_eligibility(
        replace(
            clean(),
            market_status=MarketStatus.SUSPENDED,
            market_data_delayed=True,
            continuity_status=ContinuityStatus.GAP,
            response_coverage_complete=False,
            provider_health=ProviderHealth.DEGRADED,
            explicit_ambiguity=True,
        )
    )
    assert result.status is LiveMarketEligibility.WAIT
    assert set(result.reasons) == {
        AbstentionReason.MARKET_NOT_OPEN,
        AbstentionReason.MARKET_DATA_DELAYED_OR_UNKNOWN,
        AbstentionReason.CONTINUITY_NOT_COHERENT,
        AbstentionReason.RESPONSE_COVERAGE_INCOMPLETE,
        AbstentionReason.PROVIDER_HEALTH_NOT_HEALTHY,
        AbstentionReason.EXPLICIT_AMBIGUITY,
    }


def test_heartbeat_time_cannot_refresh_quote_age_because_gate_has_no_liveness_clock_input() -> None:
    stale = replace(clean(), quote_observed_at=NOW - timedelta(seconds=30))
    result = evaluate_live_market_eligibility(stale)
    assert result.reasons == (AbstentionReason.STALE_QUOTE,)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quote_observed_at", datetime(2026, 9, 22, 10, 0)),
        ("decision_observed_at", datetime(2026, 9, 22, 10, 0)),
        ("max_quote_age", timedelta(0)),
        ("max_quote_age", timedelta(seconds=-1)),
        ("response_coverage_complete", 1),
        ("source_actionability_proven", 1),
        ("explicit_ambiguity", 0),
        ("stream_conflated", 0),
        ("requires_intermediate_granularity", 0),
        ("continuity_epoch", " epoch-7"),
        ("expected_continuity_epoch", ""),
    ],
)
def test_malformed_authority_inputs_fail_closed(field: str, value: object) -> None:
    with pytest.raises(LiveMarketAbstentionError):
        replace(clean(), **{field: value})


def test_noncanonical_input_object_fails_closed() -> None:
    with pytest.raises(LiveMarketAbstentionError):
        evaluate_live_market_eligibility(object())  # type: ignore[arg-type]


def test_randomized_gate_matches_independent_fail_closed_predicate() -> None:
    rng = random.Random(20260922)
    for _ in range(50_000):
        age_us = rng.randint(-1_000_000, 10_000_000)
        max_age_us = rng.randint(1, 10_000_000)
        status = rng.choice(list(MarketStatus))
        delayed = rng.choice([False, True, None])
        continuity = rng.choice(list(ContinuityStatus))
        complete = rng.choice([False, True])
        epoch_match = rng.choice([False, True])
        health = rng.choice(list(ProviderHealth))
        actionable = rng.choice([False, True])
        ambiguous = rng.choice([False, True])
        conflated = rng.choice([False, True])
        needs_ticks = rng.choice([False, True])

        evidence = replace(
            clean(),
            quote_observed_at=NOW - timedelta(microseconds=age_us),
            max_quote_age=timedelta(microseconds=max_age_us),
            market_status=status,
            market_data_delayed=delayed,
            continuity_status=continuity,
            response_coverage_complete=complete,
            continuity_epoch="epoch-7" if epoch_match else "epoch-6",
            provider_health=health,
            source_actionability_proven=actionable,
            explicit_ambiguity=ambiguous,
            stream_conflated=conflated,
            requires_intermediate_granularity=needs_ticks,
        )
        result = evaluate_live_market_eligibility(evidence)

        expected_wait = (
            age_us < 0
            or age_us >= max_age_us
            or status is not MarketStatus.OPEN
            or delayed is not False
            or continuity is not ContinuityStatus.COHERENT
            or not complete
            or not epoch_match
            or health is not ProviderHealth.HEALTHY
            or not actionable
            or ambiguous
            or (conflated and needs_ticks)
        )
        assert (result.status is LiveMarketEligibility.WAIT) is expected_wait
        assert result.execution_authorized is False
