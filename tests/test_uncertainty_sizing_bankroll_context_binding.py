"""Regression for #786: an absolute stake ceiling must retain bankroll context.

The sizing result is an amount, not merely a dimensionless fraction.  A consumer
must therefore be able to bind that amount to the exact bankroll authority and
currency/denominator that produced it instead of replaying the result onto a
different account or denomination.
"""

from decimal import Decimal

from autosport.uncertainty_sizing import (
    SizingAction,
    UncertaintySizingEvidence,
    UncertaintySizingPolicy,
    UncertaintySizingRequest,
    evaluate_uncertainty_sizing,
)


QUOTE_SHA = "a" * 64
CALIBRATION_SHA = "b" * 64


def _evidence() -> UncertaintySizingEvidence:
    return UncertaintySizingEvidence(
        evidence_id="uncertainty-evidence-bankroll-binding",
        candidate_id="candidate-001",
        quote_sha256=QUOTE_SHA,
        probability_model_version_id="prob-model-v7",
        calibration_bundle_sha256=CALIBRATION_SHA,
        causal_cutoff="2026-09-21T08:00:00+00:00",
        produced_at="2026-09-21T08:01:00+00:00",
        valid_until="2026-09-21T08:06:00+00:00",
        probability_lower=Decimal("0.55"),
        probability_point=Decimal("0.60"),
        probability_upper=Decimal("0.65"),
        net_win_profit_per_stake=Decimal("1"),
        evidence_refs=(
            "evidence://calibration/prob-model-v7",
            "evidence://quote/candidate-001",
        ),
    )


def _policy() -> UncertaintySizingPolicy:
    return UncertaintySizingPolicy(
        fractional_kelly=Decimal("0.25"),
        max_bankroll_fraction=Decimal("0.05"),
        max_uncertainty_width=Decimal("0.20"),
    )


def _decision(
    *,
    bankroll_id: str,
    currency: str,
    bankroll: Decimal,
):
    request = UncertaintySizingRequest(
        candidate_id="candidate-001",
        quote_sha256=QUOTE_SHA,
        decision_ts="2026-09-21T08:02:00+00:00",
        bankroll_id=bankroll_id,
        currency=currency,
        bankroll=bankroll,
    )
    decision = evaluate_uncertainty_sizing(_evidence(), request, _policy())
    assert decision.action is SizingAction.ELIGIBLE
    return request, decision


def test_absolute_stake_ceiling_binds_exact_bankroll_context() -> None:
    request, decision = _decision(
        bankroll_id="paper-eur-primary",
        currency="EUR",
        bankroll=Decimal("1000"),
    )

    assert decision.bankroll_id == request.bankroll_id
    assert decision.currency == request.currency
    assert decision.bankroll == request.bankroll
    assert decision.stake_ceiling == Decimal("25.0000")


def test_distinct_bankroll_contexts_remain_distinguishable_in_results() -> None:
    eur_request, eur_decision = _decision(
        bankroll_id="paper-eur-primary",
        currency="EUR",
        bankroll=Decimal("1000"),
    )
    usd_request, usd_decision = _decision(
        bankroll_id="paper-usd-secondary",
        currency="USD",
        bankroll=Decimal("2000"),
    )

    assert eur_decision.bankroll_id == eur_request.bankroll_id
    assert eur_decision.currency == eur_request.currency
    assert eur_decision.bankroll == eur_request.bankroll

    assert usd_decision.bankroll_id == usd_request.bankroll_id
    assert usd_decision.currency == usd_request.currency
    assert usd_decision.bankroll == usd_request.bankroll

    assert eur_decision.bankroll_id != usd_decision.bankroll_id
    assert eur_decision.currency != usd_decision.currency
    assert eur_decision.bankroll != usd_decision.bankroll
    assert eur_decision.stake_ceiling != usd_decision.stake_ceiling
