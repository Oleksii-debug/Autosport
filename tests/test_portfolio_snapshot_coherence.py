from __future__ import annotations

import itertools
from dataclasses import replace

import pytest

from autosport.portfolio_snapshot_coherence import (
    PortfolioComponentEvidence,
    PortfolioSnapshotCoherenceError,
    SnapshotCoherenceStatus,
    evaluate_portfolio_snapshot_coherence,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def evidence(
    key: str,
    *,
    evidence_id: str | None = None,
    digest: str = SHA_A,
    observed_at: str = "2026-09-21T09:00:00Z",
    available_at: str = "2026-09-21T09:00:01Z",
) -> PortfolioComponentEvidence:
    return PortfolioComponentEvidence(
        component_key=key,
        evidence_id=evidence_id or f"{key}-evidence",
        evidence_sha256=digest,
        observed_at=observed_at,
        available_at=available_at,
    )


def evaluate(items, **overrides):
    args = {
        "decision_as_of": "2026-09-21T09:00:10Z",
        "required_components": ("account-funds", "open-exposure", "market-liquidity"),
        "components": items,
        "max_age_seconds": 15,
        "max_cut_skew_seconds": 5,
    }
    args.update(overrides)
    return evaluate_portfolio_snapshot_coherence(**args)


def coherent_components():
    return (
        evidence("account-funds", digest=SHA_A, available_at="2026-09-21T09:00:05Z"),
        evidence("open-exposure", digest=SHA_B, available_at="2026-09-21T09:00:06Z"),
        evidence("market-liquidity", digest=SHA_C, available_at="2026-09-21T09:00:08Z"),
    )


def test_coherent_cut_is_order_independent_and_non_authoritative() -> None:
    expected = evaluate(coherent_components())
    assert expected.status is SnapshotCoherenceStatus.COHERENT
    assert expected.coherent is True

    payload = expected.to_dict()
    assert payload["portfolio_calculation_authority"] is False
    assert payload["risk_authority"] is False
    assert payload["execution_authority"] is False
    assert payload["real_money_execution"] is False

    for permutation in itertools.permutations(coherent_components()):
        actual = evaluate(permutation)
        assert actual.status is SnapshotCoherenceStatus.COHERENT
        assert actual.policy_sha256 == expected.policy_sha256
        assert actual.component_set_sha256 == expected.component_set_sha256
        assert actual.coherence_id == expected.coherence_id


def test_missing_required_component_waits_instead_of_using_partial_cut() -> None:
    result = evaluate(coherent_components()[:2])
    assert result.status is SnapshotCoherenceStatus.WAIT_INCOMPLETE
    assert result.reasons == ("missing:market-liquidity",)
    assert result.coherent is False


def test_unexpected_component_waits_instead_of_widening_scenario_scope() -> None:
    result = evaluate((*coherent_components(), evidence("unrequested-signal")))
    assert result.status is SnapshotCoherenceStatus.WAIT_UNEXPECTED_COMPONENT
    assert result.reasons == ("unexpected:unrequested-signal",)


def test_two_revisions_for_same_component_are_ambiguous_not_last_write_wins() -> None:
    original = coherent_components()
    replacement = evidence(
        "market-liquidity",
        evidence_id="market-liquidity-newer",
        digest="d" * 64,
        available_at="2026-09-21T09:00:09Z",
    )
    result = evaluate((*original, replacement))
    assert result.status is SnapshotCoherenceStatus.WAIT_AMBIGUOUS
    assert result.reasons == ("ambiguous:market-liquidity",)


def test_future_evidence_cannot_enter_decision_cut() -> None:
    items = list(coherent_components())
    items[0] = replace(items[0], available_at="2026-09-21T09:00:11Z")
    result = evaluate(items)
    assert result.status is SnapshotCoherenceStatus.WAIT_FUTURE
    assert result.reasons == ("future:account-funds",)


def test_stale_component_waits_even_when_other_components_are_fresh() -> None:
    items = list(coherent_components())
    items[1] = replace(
        items[1],
        observed_at="2026-09-21T08:59:49Z",
        available_at="2026-09-21T08:59:50Z",
    )
    result = evaluate(items)
    assert result.status is SnapshotCoherenceStatus.WAIT_STALE
    assert result.reasons == ("stale:open-exposure",)


def test_late_arrival_cannot_refresh_an_old_source_observation() -> None:
    items = list(coherent_components())
    items[0] = evidence(
        "account-funds",
        digest=SHA_A,
        observed_at="2026-09-21T08:00:00Z",
        available_at="2026-09-21T09:00:08Z",
    )
    result = evaluate(items, max_age_seconds=15, max_cut_skew_seconds=3600)
    assert result.status is SnapshotCoherenceStatus.WAIT_STALE
    assert result.reasons == ("stale:account-funds",)


def test_equal_arrival_times_do_not_hide_mixed_observation_cuts() -> None:
    items = (
        evidence(
            "account-funds",
            digest=SHA_A,
            observed_at="2026-09-21T09:00:00Z",
            available_at="2026-09-21T09:00:08Z",
        ),
        evidence(
            "open-exposure",
            digest=SHA_B,
            observed_at="2026-09-21T09:00:07Z",
            available_at="2026-09-21T09:00:08Z",
        ),
        evidence(
            "market-liquidity",
            digest=SHA_C,
            observed_at="2026-09-21T09:00:06Z",
            available_at="2026-09-21T09:00:08Z",
        ),
    )
    result = evaluate(items, max_age_seconds=20, max_cut_skew_seconds=5)
    assert result.status is SnapshotCoherenceStatus.WAIT_MIXED_CUT
    assert result.reasons == ("cut_skew_microseconds:7000000",)


def test_fresh_but_temporally_mixed_inputs_wait() -> None:
    items = (
        evidence("account-funds", digest=SHA_A, available_at="2026-09-21T09:00:01Z"),
        evidence("open-exposure", digest=SHA_B, available_at="2026-09-21T09:00:06Z"),
        evidence("market-liquidity", digest=SHA_C, available_at="2026-09-21T09:00:09Z"),
    )
    result = evaluate(items, max_age_seconds=20, max_cut_skew_seconds=5)
    assert result.status is SnapshotCoherenceStatus.WAIT_MIXED_CUT
    assert result.reasons == ("cut_skew_microseconds:8000000",)


def test_age_and_skew_boundaries_are_inclusive() -> None:
    items = (
        evidence(
            "account-funds",
            digest=SHA_A,
            observed_at="2026-09-21T08:59:55Z",
            available_at="2026-09-21T08:59:55Z",
        ),
        evidence("open-exposure", digest=SHA_B, available_at="2026-09-21T09:00:00Z"),
        evidence("market-liquidity", digest=SHA_C, available_at="2026-09-21T09:00:00Z"),
    )
    result = evaluate(items, max_age_seconds=15, max_cut_skew_seconds=5)
    assert result.status is SnapshotCoherenceStatus.COHERENT



def test_fractional_second_boundaries_do_not_round_down() -> None:
    stale_items = list(coherent_components())
    stale_items[0] = evidence(
        "account-funds",
        digest=SHA_A,
        observed_at="2026-09-21T08:59:54.999998Z",
        available_at="2026-09-21T08:59:54.999999Z",
    )
    stale = evaluate(stale_items, max_age_seconds=15, max_cut_skew_seconds=20)
    assert stale.status is SnapshotCoherenceStatus.WAIT_STALE

    mixed_items = (
        evidence("account-funds", digest=SHA_A, available_at="2026-09-21T09:00:01.000000Z"),
        evidence("open-exposure", digest=SHA_B, available_at="2026-09-21T09:00:06.000001Z"),
        evidence("market-liquidity", digest=SHA_C, available_at="2026-09-21T09:00:06.000000Z"),
    )
    mixed = evaluate(mixed_items, max_age_seconds=20, max_cut_skew_seconds=5)
    assert mixed.status is SnapshotCoherenceStatus.WAIT_MIXED_CUT
    assert mixed.reasons == ("cut_skew_microseconds:5000001",)

def test_causal_timestamp_and_canonical_identity_validation_fail_closed() -> None:
    with pytest.raises(PortfolioSnapshotCoherenceError, match="cannot precede"):
        evidence(
            "account-funds",
            observed_at="2026-09-21T09:00:02Z",
            available_at="2026-09-21T09:00:01Z",
        )

    with pytest.raises(PortfolioSnapshotCoherenceError, match="timezone-aware"):
        evidence("account-funds", available_at="2026-09-21T09:00:01")

    with pytest.raises(PortfolioSnapshotCoherenceError, match="lowercase SHA-256"):
        evidence("account-funds", digest="A" * 64)


def test_required_policy_is_exact_and_canonical() -> None:
    with pytest.raises(PortfolioSnapshotCoherenceError, match="cannot be empty"):
        evaluate_portfolio_snapshot_coherence(
            decision_as_of="2026-09-21T09:00:10Z",
            required_components=(),
            components=(),
            max_age_seconds=10,
            max_cut_skew_seconds=5,
        )

    with pytest.raises(PortfolioSnapshotCoherenceError, match="must be unique"):
        evaluate_portfolio_snapshot_coherence(
            decision_as_of="2026-09-21T09:00:10Z",
            required_components=("x", "x"),
            components=(),
            max_age_seconds=10,
            max_cut_skew_seconds=5,
        )

    with pytest.raises(PortfolioSnapshotCoherenceError, match="non-negative integer"):
        evaluate_portfolio_snapshot_coherence(
            decision_as_of="2026-09-21T09:00:10Z",
            required_components=("x",),
            components=(evidence("x"),),
            max_age_seconds=-1,
            max_cut_skew_seconds=5,
        )
