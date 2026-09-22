from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.execution_measurement import (
    ExecutionEffectState,
    ExecutionMeasurementError,
    ExecutionMeasurementInput,
    MatchedFragment,
    aggregate_execution_measurements,
    derive_execution_measurement,
)
from autosport.real_execution_ledger import ExecutionAction


SHA_A = "a" * 64
SHA_B = "b" * 64


def action(
    *,
    side: str = "BACK",
    odds: str = "2.00",
    stake: str = "10",
) -> ExecutionAction:
    return ExecutionAction(
        action_id="action-1",
        bookmaker_id="bookmaker-1",
        account_id="account-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side=side,
        requested_odds=Decimal(odds),
        requested_stake=Decimal(stake),
        quote_id="quote-1",
        quote_observed_at="2026-09-23T00:00:00+00:00",
        expires_at="2026-09-23T00:01:00+00:00",
    )


def fragment(
    fragment_id: str,
    price: str,
    stake: str,
    sha: str = SHA_A,
) -> MatchedFragment:
    return MatchedFragment(
        fragment_id,
        Decimal(price),
        Decimal(stake),
        sha,
    )


def inp(**changes: object) -> ExecutionMeasurementInput:
    base = ExecutionMeasurementInput(
        attempt_id="attempt-1",
        action=action(),
        effect_state=ExecutionEffectState.FULL_MATCH,
        decision_odds=Decimal("2.10"),
        clock_epoch_id="process-boot-1",
        decision_monotonic_ns=100,
        submitted_monotonic_ns=150,
        acknowledgement_monotonic_ns=250,
        execution_evidence_sha256=SHA_B,
        matched_fragments=(
            fragment("fill-1", "2.00", "10"),
        ),
        provider_event_time="2026-09-23T00:00:01Z",
    )
    return replace(base, **changes)


def test_back_adverse_delta_is_side_aware_and_exact() -> None:
    result = derive_execution_measurement(inp())
    assert result.matched_vwap == Decimal("2.00")
    assert result.adverse_odds_stake_delta == Decimal("1.00")
    assert result.decision_to_submit_ns == 50
    assert result.submit_to_acknowledgement_ns == 100
    assert result.execution_authorized is False


def test_back_better_fill_preserves_negative_favorable_delta() -> None:
    result = derive_execution_measurement(
        inp(
            matched_fragments=(
                fragment("f", "2.20", "10"),
            )
        )
    )
    assert result.adverse_odds_stake_delta == Decimal("-1.00")


def test_lay_sign_is_reversed() -> None:
    value = inp(
        action=action(side="LAY"),
        matched_fragments=(
            fragment("f", "2.20", "10"),
        ),
    )
    assert (
        derive_execution_measurement(value).adverse_odds_stake_delta
        == Decimal("1.00")
    )


def test_weighted_vwap_uses_all_fragments_without_rounding() -> None:
    value = inp(
        effect_state=ExecutionEffectState.PARTIAL_MATCH,
        matched_fragments=(
            fragment("f1", "2.01", "2"),
            fragment("f2", "2.07", "3", SHA_B),
        ),
    )
    result = derive_execution_measurement(value)
    assert result.matched_vwap == Decimal("2.046")
    assert result.matched_stake == Decimal("5")
    assert result.unresolved_stake == Decimal("5")


@pytest.mark.parametrize(
    "state",
    [
        ExecutionEffectState.ACCEPTED_UNMATCHED,
        ExecutionEffectState.DELAYED,
        ExecutionEffectState.UNKNOWN,
        ExecutionEffectState.REJECTED,
    ],
)
def test_nonmatched_states_never_mint_zero_slippage(
    state: ExecutionEffectState,
) -> None:
    value = inp(
        effect_state=state,
        matched_fragments=(),
        acknowledgement_monotonic_ns=(
            None
            if state is ExecutionEffectState.UNKNOWN
            else 250
        ),
    )
    result = derive_execution_measurement(value)
    assert result.matched_vwap is None
    assert result.adverse_odds_stake_delta is None
    assert result.slippage_observed is False


def test_unknown_preserves_full_unresolved_stake_and_censored_ack_latency() -> None:
    result = derive_execution_measurement(
        inp(
            effect_state=ExecutionEffectState.UNKNOWN,
            matched_fragments=(),
            acknowledgement_monotonic_ns=None,
        )
    )
    assert result.unresolved_stake == Decimal("10")
    assert result.submit_to_acknowledgement_ns is None


def test_rejected_is_resolved_no_fill_but_not_slippage_sample() -> None:
    result = derive_execution_measurement(
        inp(
            effect_state=ExecutionEffectState.REJECTED,
            matched_fragments=(),
        )
    )
    assert result.unresolved_stake == Decimal("0")
    assert result.adverse_odds_stake_delta is None


def test_partial_requires_strict_partial_match() -> None:
    with pytest.raises(ExecutionMeasurementError):
        inp(
            effect_state=ExecutionEffectState.PARTIAL_MATCH,
            matched_fragments=(),
        )
    with pytest.raises(ExecutionMeasurementError):
        inp(
            effect_state=ExecutionEffectState.PARTIAL_MATCH,
            matched_fragments=(
                fragment("f", "2", "10"),
            ),
        )


def test_full_match_requires_exact_requested_stake() -> None:
    with pytest.raises(ExecutionMeasurementError):
        inp(
            matched_fragments=(
                fragment("f", "2", "9"),
            )
        )


def test_nonmatched_state_rejects_matched_fragment() -> None:
    with pytest.raises(ExecutionMeasurementError):
        inp(
            effect_state=ExecutionEffectState.UNKNOWN,
        )


def test_duplicate_fragment_identity_is_rejected() -> None:
    with pytest.raises(ExecutionMeasurementError):
        inp(
            effect_state=ExecutionEffectState.PARTIAL_MATCH,
            matched_fragments=(
                fragment("f", "2", "2"),
                fragment("f", "2.1", "3"),
            ),
        )


def test_matched_stake_cannot_exceed_requested() -> None:
    with pytest.raises(ExecutionMeasurementError):
        inp(
            matched_fragments=(
                fragment("f", "2", "11"),
            )
        )


def test_only_unknown_may_omit_ack_clock() -> None:
    with pytest.raises(ExecutionMeasurementError):
        inp(
            acknowledgement_monotonic_ns=None,
        )


def test_monotonic_clock_order_is_enforced() -> None:
    with pytest.raises(ExecutionMeasurementError):
        inp(
            submitted_monotonic_ns=99,
        )
    with pytest.raises(ExecutionMeasurementError):
        inp(
            acknowledgement_monotonic_ns=149,
        )


def test_float_money_is_rejected() -> None:
    with pytest.raises(ExecutionMeasurementError):
        MatchedFragment(
            "f",
            2.0,  # type: ignore[arg-type]
            Decimal("1"),
            SHA_A,
        )
    with pytest.raises(ExecutionMeasurementError):
        inp(
            decision_odds=2.1,  # type: ignore[arg-type]
        )


def test_noncanonical_hash_is_rejected() -> None:
    with pytest.raises(ExecutionMeasurementError):
        inp(
            execution_evidence_sha256="A" * 64,
        )


def test_aggregate_keeps_full_attempt_denominator_and_coverage() -> None:
    full = derive_execution_measurement(inp())
    unknown = derive_execution_measurement(
        inp(
            attempt_id="attempt-2",
            action=replace(
                action(),
                action_id="action-2",
            ),
            effect_state=ExecutionEffectState.UNKNOWN,
            matched_fragments=(),
            acknowledgement_monotonic_ns=None,
        )
    )
    result = aggregate_execution_measurements(
        (full, unknown)
    )
    assert result.attempt_count == 2
    assert result.slippage_observed_attempt_count == 1
    assert result.total_requested_stake == Decimal("20")
    assert result.total_matched_stake == Decimal("10")
    assert (
        result.realized_slippage_stake_coverage
        == Decimal("0.5")
    )
    assert (
        result.total_adverse_odds_stake_delta
        == Decimal("1.00")
    )


def test_aggregate_rejects_duplicate_attempt_id() -> None:
    value = derive_execution_measurement(inp())
    with pytest.raises(ExecutionMeasurementError):
        aggregate_execution_measurements(
            (value, value)
        )


def test_assertion_digest_is_deterministic_and_binds_fragments() -> None:
    first = derive_execution_measurement(inp())
    second = derive_execution_measurement(inp())
    changed = derive_execution_measurement(
        inp(
            matched_fragments=(
                fragment("fill-2", "2", "10"),
            )
        )
    )
    assert first.assertion_digest == second.assertion_digest
    assert first.assertion_digest != changed.assertion_digest


def test_provider_wall_clock_is_preserved_but_not_used_for_latency() -> None:
    result = derive_execution_measurement(
        inp(
            provider_event_time="1900-01-01T00:00:00Z",
        )
    )
    assert (
        result.provider_event_time
        == "1900-01-01T00:00:00Z"
    )
    assert result.decision_to_submit_ns == 50
    assert result.submit_to_acknowledgement_ns == 100
