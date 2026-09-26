from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext

import pytest

from autosport.anchor_feasibility import (
    AcquisitionState,
    AnchorFeasibilityError,
    AnchorObservation,
    AnchorScope,
    AnchorWindowClosure,
    FeasibilityDisposition,
    evaluate_anchor_feasibility,
)


BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)
SCOPE = AnchorScope(
    sport="football",
    league="EPL",
    market="MATCH_ODDS",
    provider="reference-provider",
    currency="EUR",
)
SHA_A = "a" * 64
SHA_B = "b" * 64


def obs(
    sequence: int,
    state: AcquisitionState,
    *,
    hour: int | None = None,
    event_id: str | None = None,
    slack: str | None = None,
    liquidity: str | None = None,
    cost: str = "0.10",
    capital: str = "10",
    held_hours: str = "0.5",
    scope: AnchorScope = SCOPE,
    acquisition_id: str | None = None,
    source_sha256: str = SHA_A,
) -> AnchorObservation:
    return AnchorObservation(
        sequence=sequence,
        acquisition_id=acquisition_id or f"acq-{sequence}",
        scope=scope,
        state=state,
        observed_at=BASE + timedelta(hours=sequence if hour is None else hour),
        source_sha256=source_sha256,
        applicable_cost=Decimal(cost),
        capital_amount=Decimal(capital),
        capital_held_hours=Decimal(held_hours),
        event_id=event_id,
        reaction_slack_seconds=None if slack is None else Decimal(slack),
        displayed_liquidity=None if liquidity is None else Decimal(liquidity),
    )


def evaluate(rows, *, end_days: int = 14, review_days: int = 15):
    rows = tuple(rows)
    window_end = BASE + timedelta(days=end_days)
    closure = AnchorWindowClosure(
        scope=SCOPE,
        window_start=BASE,
        window_end=window_end,
        first_sequence=1,
        last_sequence=rows[-1].sequence,
        closed_at=window_end,
        source_universe_sha256=SHA_B,
        closure_evidence_sha256=SHA_A,
    )
    return evaluate_anchor_feasibility(
        scope=SCOPE,
        window_start=BASE,
        window_end=window_end,
        review_as_of=BASE + timedelta(days=review_days),
        observations=rows,
        window_closure=closure,
    )


def test_complete_fourteen_day_review_can_only_continue_evidence() -> None:
    report = evaluate(
        [
            obs(1, AcquisitionState.OBSERVED, event_id="E1", slack="4", liquidity="50"),
            obs(2, AcquisitionState.ZERO_RESULT),
            obs(3, AcquisitionState.PROVIDER_FAILURE),
            obs(4, AcquisitionState.LOCAL_FAILURE),
            obs(5, AcquisitionState.STOPPED),
        ]
    )
    assert report.disposition is FeasibilityDisposition.CONTINUE_EVIDENCE
    assert report.terminal_coverage_complete is True
    assert report.state_counts == (
        ("OBSERVED", 1),
        ("ZERO_RESULT", 1),
        ("PROVIDER_FAILURE", 1),
        ("LOCAL_FAILURE", 1),
        ("STOPPED", 1),
        ("PENDING", 0),
    )
    assert report.ranking_authority is False
    assert report.winner_authority is False
    assert report.promotion_authority is False
    assert report.execution_authority is False
    assert report.external_validity_authority is False
    assert report.real_money_authority is False


@pytest.mark.parametrize(
    "field_name",
    [
        "ranking_authority",
        "winner_authority",
        "promotion_authority",
        "execution_authority",
        "external_validity_authority",
        "real_money_authority",
    ],
)
def test_public_report_constructor_cannot_mint_downstream_authority(
    field_name: str,
) -> None:
    report = evaluate([obs(1, AcquisitionState.ZERO_RESULT)])
    with pytest.raises(AnchorFeasibilityError, match="cannot grant downstream authority"):
        replace(report, **{field_name: True})
    with pytest.raises(AnchorFeasibilityError, match="cannot grant downstream authority"):
        replace(report, **{field_name: 1})


def test_pending_blocks_terminal_coverage_even_after_window_end() -> None:
    report = evaluate([obs(1, AcquisitionState.PENDING)])
    assert report.disposition is FeasibilityDisposition.INCOMPLETE
    assert report.terminal_coverage_complete is False


def test_review_before_window_end_is_incomplete() -> None:
    report = evaluate_anchor_feasibility(
        scope=SCOPE,
        window_start=BASE,
        window_end=BASE + timedelta(days=14),
        review_as_of=BASE + timedelta(days=7),
        observations=[obs(1, AcquisitionState.ZERO_RESULT)],
    )
    assert report.disposition is FeasibilityDisposition.INCOMPLETE


def test_short_complete_window_collects_more() -> None:
    report = evaluate([obs(1, AcquisitionState.ZERO_RESULT)], end_days=13, review_days=14)
    assert report.disposition is FeasibilityDisposition.COLLECT_MORE


def test_fourteen_days_minus_one_microsecond_does_not_cross_checkpoint() -> None:
    window_end = BASE + timedelta(days=14) - timedelta(microseconds=1)
    row = obs(1, AcquisitionState.ZERO_RESULT)
    closure = AnchorWindowClosure(
        scope=SCOPE,
        window_start=BASE,
        window_end=window_end,
        first_sequence=1,
        last_sequence=1,
        closed_at=window_end,
        source_universe_sha256=SHA_B,
        closure_evidence_sha256=SHA_A,
    )
    report = evaluate_anchor_feasibility(
        scope=SCOPE,
        window_start=BASE,
        window_end=window_end,
        review_as_of=BASE + timedelta(days=15),
        observations=[row],
        window_closure=closure,
    )
    assert report.disposition is FeasibilityDisposition.COLLECT_MORE
    assert report.review_window_days < Decimal("14")


def test_distinct_events_not_quote_count_define_recurrence() -> None:
    report = evaluate(
        [
            obs(1, AcquisitionState.OBSERVED, event_id="E1", slack="8", liquidity="100"),
            obs(2, AcquisitionState.OBSERVED, event_id="E1", slack="3", liquidity="80"),
            obs(3, AcquisitionState.OBSERVED, event_id="E2", slack="10", liquidity="40"),
        ]
    )
    assert report.acquisition_count == 3
    assert report.distinct_event_count == 2
    # Per event, use conservative minima: slack [3,10], liquidity [80,40].
    assert report.reaction_slack_p10_seconds == Decimal("3")
    assert report.reaction_slack_median_seconds == Decimal("6.5")
    assert report.displayed_liquidity_min == Decimal("40")
    assert report.displayed_liquidity_median == Decimal("60")


def test_failures_and_zero_results_remain_in_cost_and_capital_time_truth() -> None:
    report = evaluate(
        [
            obs(1, AcquisitionState.ZERO_RESULT, cost="1.20", capital="10", held_hours="0.5"),
            obs(2, AcquisitionState.PROVIDER_FAILURE, cost="2.30", capital="20", held_hours="0.25"),
            obs(3, AcquisitionState.OBSERVED, event_id="E1", slack="1", liquidity="5", cost="0.50", capital="4", held_hours="2"),
        ]
    )
    assert report.total_applicable_cost == Decimal("4.00")
    assert report.total_capital_time_currency_hours == Decimal("18.00")
    assert report.cost_per_observed_opportunity == Decimal("4.00")


def test_capital_time_is_exact_under_low_ambient_decimal_precision() -> None:
    old = getcontext().prec
    try:
        getcontext().prec = 3
        report = evaluate(
            [
                obs(
                    1,
                    AcquisitionState.OBSERVED,
                    event_id="E1",
                    slack="0.1",
                    liquidity="1",
                    capital="123456789.123456789",
                    held_hours="0.123456789123456789",
                    cost="0",
                )
            ]
        )
    finally:
        getcontext().prec = old
    assert report.total_capital_time_currency_hours == Decimal(
        "15241578.780673678515622620750190521"
    )


def test_decimal_spelling_scale_does_not_change_evidence_identity() -> None:
    a = evaluate(
        [obs(1, AcquisitionState.OBSERVED, event_id="E1", slack="1.0", liquidity="10.00", cost="0.10", capital="2.0", held_hours="0.50")]
    )
    b = evaluate(
        [obs(1, AcquisitionState.OBSERVED, event_id="E1", slack="1.00", liquidity="10.0", cost="0.100", capital="2.00", held_hours="0.500")]
    )
    assert a.evidence_sha256 == b.evidence_sha256


@pytest.mark.parametrize("bad", [True, 1, 1.0])
def test_binary_float_and_bool_int_alias_money_inputs_fail_closed(bad) -> None:
    with pytest.raises(AnchorFeasibilityError):
        AnchorObservation(
            sequence=1,
            acquisition_id="acq",
            scope=SCOPE,
            state=AcquisitionState.ZERO_RESULT,
            observed_at=BASE,
            source_sha256=SHA_A,
            applicable_cost=bad,  # type: ignore[arg-type]
            capital_amount=Decimal("0"),
            capital_held_hours=Decimal("0"),
        )


def test_sequence_gap_fails_closed() -> None:
    with pytest.raises(AnchorFeasibilityError, match="contiguous"):
        evaluate([obs(1, AcquisitionState.ZERO_RESULT), obs(3, AcquisitionState.ZERO_RESULT)])


def test_sequence_must_start_at_one() -> None:
    with pytest.raises(AnchorFeasibilityError, match="contiguous"):
        evaluate([obs(2, AcquisitionState.ZERO_RESULT)])


def test_timestamp_rollback_fails_closed() -> None:
    with pytest.raises(AnchorFeasibilityError, match="rollback"):
        evaluate(
            [
                obs(1, AcquisitionState.ZERO_RESULT, hour=10),
                obs(2, AcquisitionState.ZERO_RESULT, hour=9),
            ]
        )


def test_out_of_window_observation_fails_closed() -> None:
    row = AnchorObservation(
        sequence=1,
        acquisition_id="outside",
        scope=SCOPE,
        state=AcquisitionState.ZERO_RESULT,
        observed_at=BASE - timedelta(seconds=1),
        source_sha256=SHA_A,
        applicable_cost=Decimal("0"),
        capital_amount=Decimal("0"),
        capital_held_hours=Decimal("0"),
    )
    with pytest.raises(AnchorFeasibilityError, match="outside"):
        evaluate([row])


def test_scope_drift_fails_closed() -> None:
    other = AnchorScope("football", "EPL", "MATCH_ODDS", "other-provider", "EUR")
    with pytest.raises(AnchorFeasibilityError, match="scope drift"):
        evaluate([obs(1, AcquisitionState.ZERO_RESULT, scope=other)])


def test_acquisition_id_replay_fails_closed() -> None:
    with pytest.raises(AnchorFeasibilityError, match="replay"):
        evaluate(
            [
                obs(1, AcquisitionState.ZERO_RESULT, acquisition_id="same"),
                obs(2, AcquisitionState.PROVIDER_FAILURE, acquisition_id="same"),
            ]
        )


def test_observed_requires_event_slack_and_liquidity() -> None:
    with pytest.raises(AnchorFeasibilityError):
        obs(1, AcquisitionState.OBSERVED, event_id="E1", slack=None, liquidity="1")


@pytest.mark.parametrize(
    "state",
    [
        AcquisitionState.ZERO_RESULT,
        AcquisitionState.PROVIDER_FAILURE,
        AcquisitionState.LOCAL_FAILURE,
        AcquisitionState.STOPPED,
        AcquisitionState.PENDING,
    ],
)
def test_non_observed_states_cannot_smuggle_market_metrics(state: AcquisitionState) -> None:
    with pytest.raises(AnchorFeasibilityError):
        obs(1, state, event_id="E1", slack="1", liquidity="2")


def test_bad_source_digest_fails_closed() -> None:
    with pytest.raises(AnchorFeasibilityError, match="SHA-256"):
        obs(1, AcquisitionState.ZERO_RESULT, source_sha256="ABC")


def test_naive_timestamps_fail_closed() -> None:
    with pytest.raises(AnchorFeasibilityError, match="timezone-aware"):
        AnchorObservation(
            sequence=1,
            acquisition_id="naive",
            scope=SCOPE,
            state=AcquisitionState.ZERO_RESULT,
            observed_at=datetime(2026, 9, 1),
            source_sha256=SHA_A,
            applicable_cost=Decimal("0"),
            capital_amount=Decimal("0"),
            capital_held_hours=Decimal("0"),
        )


def test_no_observed_events_preserves_zero_result_truth_without_inventing_metrics() -> None:
    report = evaluate(
        [
            obs(1, AcquisitionState.ZERO_RESULT, cost="1"),
            obs(2, AcquisitionState.ZERO_RESULT, cost="2"),
        ]
    )
    assert report.distinct_event_count == 0
    assert report.reaction_slack_p10_seconds is None
    assert report.reaction_slack_median_seconds is None
    assert report.displayed_liquidity_min is None
    assert report.displayed_liquidity_median is None
    assert report.cost_per_observed_opportunity is None
    assert report.total_applicable_cost == Decimal("3")


def test_evidence_identity_changes_on_provider_failure_vs_zero_result() -> None:
    zero = evaluate([obs(1, AcquisitionState.ZERO_RESULT)])
    fail = evaluate([obs(1, AcquisitionState.PROVIDER_FAILURE)])
    assert zero.evidence_sha256 != fail.evidence_sha256


def test_evidence_identity_changes_on_scope_even_with_same_rows() -> None:
    other = AnchorScope("football", "EPL", "MATCH_ODDS", "reference-provider", "GBP")
    row = obs(1, AcquisitionState.ZERO_RESULT, scope=other)
    other_report = evaluate_anchor_feasibility(
        scope=other,
        window_start=BASE,
        window_end=BASE + timedelta(days=14),
        review_as_of=BASE + timedelta(days=15),
        observations=[row],
    )
    base_report = evaluate([obs(1, AcquisitionState.ZERO_RESULT)])
    assert other_report.evidence_sha256 != base_report.evidence_sha256

def test_future_observation_after_review_as_of_fails_closed() -> None:
    future_row = obs(
        1,
        AcquisitionState.ZERO_RESULT,
        hour=10 * 24,
    )
    with pytest.raises(AnchorFeasibilityError, match="review_as_of"):
        evaluate_anchor_feasibility(
            scope=SCOPE,
            window_start=BASE,
            window_end=BASE + timedelta(days=14),
            review_as_of=BASE + timedelta(days=7),
            observations=[future_row],
        )


def test_fixed_fourteen_day_horizon_cannot_be_relaxed() -> None:
    with pytest.raises(AnchorFeasibilityError, match="fixed at 14"):
        evaluate_anchor_feasibility(
            scope=SCOPE,
            window_start=BASE,
            window_end=BASE + timedelta(days=1),
            review_as_of=BASE + timedelta(days=2),
            observations=[obs(1, AcquisitionState.ZERO_RESULT)],
            minimum_review_days=Decimal("0"),
        )


def test_missing_window_closure_cannot_prove_terminal_coverage() -> None:
    report = evaluate_anchor_feasibility(
        scope=SCOPE,
        window_start=BASE,
        window_end=BASE + timedelta(days=14),
        review_as_of=BASE + timedelta(days=15),
        observations=[obs(1, AcquisitionState.ZERO_RESULT)],
    )
    assert report.terminal_coverage_complete is False
    assert report.disposition is FeasibilityDisposition.INCOMPLETE


def test_window_closure_must_bind_exact_sequence_and_be_causally_available() -> None:
    row = obs(1, AcquisitionState.ZERO_RESULT)
    wrong_sequence = AnchorWindowClosure(
        scope=SCOPE,
        window_start=BASE,
        window_end=BASE + timedelta(days=14),
        first_sequence=1,
        last_sequence=2,
        closed_at=BASE + timedelta(days=14),
        source_universe_sha256=SHA_B,
        closure_evidence_sha256=SHA_A,
    )
    with pytest.raises(AnchorFeasibilityError, match="sequence range"):
        evaluate_anchor_feasibility(
            scope=SCOPE,
            window_start=BASE,
            window_end=BASE + timedelta(days=14),
            review_as_of=BASE + timedelta(days=15),
            observations=[row],
            window_closure=wrong_sequence,
        )

    future_closure = AnchorWindowClosure(
        scope=SCOPE,
        window_start=BASE,
        window_end=BASE + timedelta(days=14),
        first_sequence=1,
        last_sequence=1,
        closed_at=BASE + timedelta(days=16),
        source_universe_sha256=SHA_B,
        closure_evidence_sha256=SHA_A,
    )
    with pytest.raises(AnchorFeasibilityError, match="not causally available"):
        evaluate_anchor_feasibility(
            scope=SCOPE,
            window_start=BASE,
            window_end=BASE + timedelta(days=14),
            review_as_of=BASE + timedelta(days=15),
            observations=[row],
            window_closure=future_closure,
        )

