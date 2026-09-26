from __future__ import annotations

import decimal
from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.portfolio_live_scenario import (
    FrozenLiveScenarioSpec,
    LivePortfolioScenarioError,
    LivePositionScenarioComponent,
    evaluate_live_portfolio_scenario,
)


OPENED = "2026-09-21T18:00:00+00:00"
CLOSED = "2026-09-21T18:00:02+00:00"
PORTFOLIO_SHA = "a" * 64


def _spec(**changes) -> FrozenLiveScenarioSpec:
    values = dict(
        scenario_id="scenario-1",
        batch_id="batch-1",
        portfolio_state_sha256=PORTFOLIO_SHA,
        generation=7,
        expected_position_ids=("position-a", "position-b"),
        batch_opened_at=OPENED,
        batch_closed_at=CLOSED,
        max_observation_skew_microseconds=1_000_000,
    )
    values.update(changes)
    return FrozenLiveScenarioSpec(**values)


def _component(
    position_id: str,
    *,
    provider_id: str,
    observed_at: str,
    committed_at: str,
    capital: str = "10",
    loss: str = "8",
    state_char: str = "b",
    **changes,
) -> LivePositionScenarioComponent:
    values = dict(
        position_id=position_id,
        event_id=f"event-{position_id}",
        market_id=f"market-{position_id}",
        provider_id=provider_id,
        account_id=f"account-{provider_id}",
        batch_id="batch-1",
        portfolio_state_sha256=PORTFOLIO_SHA,
        generation=7,
        position_state_sha256=state_char * 64,
        observed_at=observed_at,
        committed_at=committed_at,
        capital_at_risk=Decimal(capital),
        conservative_loss_upper_bound=Decimal(loss),
    )
    values.update(changes)
    return LivePositionScenarioComponent(**values)


def _pair():
    return (
        _component(
            "position-a",
            provider_id="provider-a",
            observed_at="2026-09-21T18:00:00.500000+00:00",
            committed_at="2026-09-21T18:00:01.100000+00:00",
            capital="10.00",
            loss="8.00",
            state_char="b",
        ),
        _component(
            "position-b",
            provider_id="provider-b",
            observed_at="2026-09-21T18:00:01+00:00",
            committed_at="2026-09-21T18:00:01.500000+00:00",
            capital="20.00",
            loss="12.00",
            state_char="c",
        ),
    )


def test_complete_vector_aggregates_conservative_risk_exactly() -> None:
    evidence = evaluate_live_portfolio_scenario(_spec(), _pair())

    assert evidence.position_count == 2
    assert evidence.total_capital_at_risk == Decimal("30.00")
    assert (
        evidence.conservative_componentwise_loss_upper_bound
        == Decimal("20.00")
    )
    assert evidence.observation_skew_microseconds == 500_000
    assert evidence.structural_vector_complete is True
    assert len(evidence.evidence_sha256) == 64


def test_input_order_does_not_change_evidence_identity() -> None:
    first, second = _pair()

    forward = evaluate_live_portfolio_scenario(_spec(), (first, second))
    reverse = evaluate_live_portfolio_scenario(_spec(), (second, first))

    assert forward.evidence_sha256 == reverse.evidence_sha256


def test_decimal_scale_does_not_change_evidence_identity() -> None:
    first, second = _pair()
    baseline = evaluate_live_portfolio_scenario(_spec(), (first, second))
    scaled = evaluate_live_portfolio_scenario(
        _spec(),
        (
            replace(
                first,
                capital_at_risk=Decimal("10.0000"),
                conservative_loss_upper_bound=Decimal("8.000"),
            ),
            replace(
                second,
                capital_at_risk=Decimal("20.0"),
                conservative_loss_upper_bound=Decimal("12.00000"),
            ),
        ),
    )

    assert scaled.evidence_sha256 == baseline.evidence_sha256


def test_missing_declared_position_fails_closed() -> None:
    first, _ = _pair()
    with pytest.raises(LivePortfolioScenarioError, match="exactly cover"):
        evaluate_live_portfolio_scenario(_spec(), (first,))


def test_unexpected_position_fails_closed() -> None:
    first, second = _pair()
    unexpected = replace(
        second,
        position_id="position-c",
        position_state_sha256="d" * 64,
    )
    with pytest.raises(LivePortfolioScenarioError, match="exactly cover"):
        evaluate_live_portfolio_scenario(_spec(), (first, unexpected))


def test_duplicate_component_identity_fails_closed() -> None:
    first, _ = _pair()
    with pytest.raises(LivePortfolioScenarioError, match="duplicate position_id"):
        evaluate_live_portfolio_scenario(_spec(), (first, first))


def test_duplicate_declared_identity_is_rejected() -> None:
    with pytest.raises(
        LivePortfolioScenarioError, match="expected_position_ids must be unique"
    ):
        _spec(expected_position_ids=("position-a", "position-a"))


def test_mixed_portfolio_state_is_rejected() -> None:
    first, second = _pair()
    second = replace(second, portfolio_state_sha256="d" * 64)
    with pytest.raises(
        LivePortfolioScenarioError, match="portfolio state mismatches"
    ):
        evaluate_live_portfolio_scenario(_spec(), (first, second))


def test_mixed_generation_is_rejected() -> None:
    first, second = _pair()
    second = replace(second, generation=8)
    with pytest.raises(
        LivePortfolioScenarioError, match="generation mismatches"
    ):
        evaluate_live_portfolio_scenario(_spec(), (first, second))


def test_mixed_batch_id_is_rejected() -> None:
    first, second = _pair()
    second = replace(second, batch_id="batch-2")
    with pytest.raises(LivePortfolioScenarioError, match="batch_id mismatches"):
        evaluate_live_portfolio_scenario(_spec(), (first, second))


def test_observation_before_batch_open_is_rejected() -> None:
    first, second = _pair()
    first = replace(first, observed_at="2026-09-21T17:59:59.999999+00:00")
    with pytest.raises(
        LivePortfolioScenarioError, match="outside frozen batch interval"
    ):
        evaluate_live_portfolio_scenario(_spec(), (first, second))


def test_observation_after_batch_close_is_rejected() -> None:
    first, second = _pair()
    second = replace(
        second,
        observed_at="2026-09-21T18:00:02.000001+00:00",
        committed_at="2026-09-21T18:00:02.000001+00:00",
    )
    with pytest.raises(
        LivePortfolioScenarioError, match="outside frozen batch interval"
    ):
        evaluate_live_portfolio_scenario(_spec(), (first, second))


def test_commit_before_observation_is_rejected_at_component_boundary() -> None:
    with pytest.raises(
        LivePortfolioScenarioError, match="must not precede observed_at"
    ):
        _component(
            "position-a",
            provider_id="provider-a",
            observed_at="2026-09-21T18:00:01+00:00",
            committed_at="2026-09-21T18:00:00.999999+00:00",
        )


def test_commit_after_frozen_batch_close_is_rejected() -> None:
    first, second = _pair()
    second = replace(
        second, committed_at="2026-09-21T18:00:02.000001+00:00"
    )
    with pytest.raises(
        LivePortfolioScenarioError, match="later than frozen batch close"
    ):
        evaluate_live_portfolio_scenario(_spec(), (first, second))


def test_observation_skew_above_policy_is_rejected() -> None:
    with pytest.raises(
        LivePortfolioScenarioError, match="observation skew exceeds"
    ):
        evaluate_live_portfolio_scenario(
            _spec(max_observation_skew_microseconds=499_999), _pair()
        )


def test_observation_skew_exact_boundary_is_admitted() -> None:
    evidence = evaluate_live_portfolio_scenario(
        _spec(max_observation_skew_microseconds=500_000), _pair()
    )
    assert evidence.observation_skew_microseconds == 500_000


@pytest.mark.parametrize(
    "field,value",
    [
        ("capital_at_risk", 10.0),
        ("conservative_loss_upper_bound", 8.0),
        ("capital_at_risk", Decimal("NaN")),
        ("capital_at_risk", Decimal("Infinity")),
        ("capital_at_risk", Decimal("-1")),
        ("conservative_loss_upper_bound", Decimal("-1")),
    ],
)
def test_money_domain_rejects_float_nonfinite_and_negative(field, value) -> None:
    kwargs = {field: value}
    with pytest.raises(LivePortfolioScenarioError, match="finite non-negative Decimal"):
        _component(
            "position-a",
            provider_id="provider-a",
            observed_at="2026-09-21T18:00:00+00:00",
            committed_at="2026-09-21T18:00:00+00:00",
            **kwargs,
        )


def test_loss_bound_cannot_exceed_declared_capital_at_risk() -> None:
    with pytest.raises(LivePortfolioScenarioError, match="cannot exceed"):
        _component(
            "position-a",
            provider_id="provider-a",
            observed_at="2026-09-21T18:00:00+00:00",
            committed_at="2026-09-21T18:00:00+00:00",
            capital="5",
            loss="5.01",
        )


def test_bool_generation_is_rejected_as_integer_alias() -> None:
    with pytest.raises(LivePortfolioScenarioError, match="positive integer"):
        _spec(generation=True)


def test_bool_skew_policy_is_rejected_as_integer_alias() -> None:
    with pytest.raises(
        LivePortfolioScenarioError, match="non-negative integer"
    ):
        _spec(max_observation_skew_microseconds=False)


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(LivePortfolioScenarioError, match="timezone-aware"):
        _spec(batch_opened_at="2026-09-21T18:00:00")


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-21T18:00:02.0000001+00:00",
        "2026-09-21T18:00:02,0000001+00:00",
    ],
)
def test_submicrosecond_component_timestamp_is_rejected(timestamp: str) -> None:
    with pytest.raises(
        LivePortfolioScenarioError,
        match="must not exceed microsecond precision",
    ):
        _component(
            "position-a",
            provider_id="provider-a",
            observed_at=timestamp,
            committed_at=timestamp,
        )


def test_submicrosecond_batch_boundary_is_rejected() -> None:
    with pytest.raises(
        LivePortfolioScenarioError,
        match="must not exceed microsecond precision",
    ):
        _spec(batch_closed_at="2026-09-21T18:00:02.0000001+00:00")


def test_equivalent_timezone_spellings_have_same_identity() -> None:
    first, second = _pair()
    baseline = evaluate_live_portfolio_scenario(_spec(), (first, second))
    equivalent_spec = _spec(
        batch_opened_at="2026-09-21T20:00:00+02:00",
        batch_closed_at="2026-09-21T20:00:02+02:00",
    )
    equivalent_pair = (
        replace(
            first,
            observed_at="2026-09-21T20:00:00.500000+02:00",
            committed_at="2026-09-21T20:00:01.100000+02:00",
        ),
        replace(
            second,
            observed_at="2026-09-21T20:00:01+02:00",
            committed_at="2026-09-21T20:00:01.500000+02:00",
        ),
    )
    equivalent = evaluate_live_portfolio_scenario(
        equivalent_spec, equivalent_pair
    )
    assert equivalent.evidence_sha256 == baseline.evidence_sha256


def test_ambient_decimal_context_cannot_round_aggregates_or_identity() -> None:
    first = _component(
        "position-a",
        provider_id="provider-a",
        observed_at="2026-09-21T18:00:00.500000+00:00",
        committed_at="2026-09-21T18:00:01+00:00",
        capital="12345678901234567890.123456789",
        loss="10000000000000000000.000000001",
        state_char="b",
    )
    second = _component(
        "position-b",
        provider_id="provider-b",
        observed_at="2026-09-21T18:00:01+00:00",
        committed_at="2026-09-21T18:00:01.500000+00:00",
        capital="0.000000001",
        loss="0.000000001",
        state_char="c",
    )

    with decimal.localcontext() as context:
        context.prec = 6
        context.rounding = decimal.ROUND_DOWN
        low = evaluate_live_portfolio_scenario(_spec(), (first, second))
    with decimal.localcontext() as context:
        context.prec = 50
        context.rounding = decimal.ROUND_UP
        high = evaluate_live_portfolio_scenario(_spec(), (first, second))

    assert low.total_capital_at_risk == Decimal(
        "12345678901234567890.123456790"
    )
    assert low.conservative_componentwise_loss_upper_bound == Decimal(
        "10000000000000000000.000000002"
    )
    assert low.evidence_sha256 == high.evidence_sha256


def test_economic_change_changes_evidence_identity() -> None:
    first, second = _pair()
    baseline = evaluate_live_portfolio_scenario(_spec(), (first, second))
    changed = evaluate_live_portfolio_scenario(
        _spec(),
        (
            first,
            replace(
                second,
                capital_at_risk=Decimal("20.01"),
                conservative_loss_upper_bound=Decimal("12.01"),
            ),
        ),
    )
    assert changed.evidence_sha256 != baseline.evidence_sha256


def test_positive_result_never_upgrades_external_or_execution_authority() -> None:
    evidence = evaluate_live_portfolio_scenario(_spec(), _pair())

    assert evidence.source_authority_proven is False
    assert evidence.portfolio_universe_authoritative is False
    assert evidence.cross_provider_atomicity_proven is False
    assert evidence.terminal_outcome_exactness_proven is False
    assert evidence.grants_sizing_authority is False
    assert evidence.grants_execution_authority is False
    assert evidence.grants_settlement_authority is False


def test_explicit_empty_declared_vector_is_structurally_zero_not_authoritative() -> None:
    spec = _spec(expected_position_ids=())
    evidence = evaluate_live_portfolio_scenario(spec, ())

    assert evidence.position_count == 0
    assert evidence.total_capital_at_risk == Decimal("0")
    assert evidence.conservative_componentwise_loss_upper_bound == Decimal("0")
    assert evidence.structural_vector_complete is True
    assert evidence.portfolio_universe_authoritative is False
    assert evidence.grants_execution_authority is False


def test_repeated_local_hour_offsets_use_real_instant_order() -> None:
    with pytest.raises(
        LivePortfolioScenarioError, match="must not precede observed_at"
    ):
        _component(
            "position-a",
            provider_id="provider-a",
            observed_at="2026-11-01T01:15:00-05:00",
            committed_at="2026-11-01T01:45:00-04:00",
        )


def test_subclassed_component_is_not_accepted_as_exact_contract_input() -> None:
    class DerivedComponent(LivePositionScenarioComponent):
        pass

    first, second = _pair()
    derived = DerivedComponent(
        position_id=first.position_id,
        event_id=first.event_id,
        market_id=first.market_id,
        provider_id=first.provider_id,
        account_id=first.account_id,
        batch_id=first.batch_id,
        portfolio_state_sha256=first.portfolio_state_sha256,
        generation=first.generation,
        position_state_sha256=first.position_state_sha256,
        observed_at=first.observed_at,
        committed_at=first.committed_at,
        capital_at_risk=first.capital_at_risk,
        conservative_loss_upper_bound=first.conservative_loss_upper_bound,
    )

    with pytest.raises(
        LivePortfolioScenarioError,
        match="tuple of LivePositionScenarioComponent",
    ):
        evaluate_live_portfolio_scenario(_spec(), (derived, second))
