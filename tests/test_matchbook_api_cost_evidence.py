from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect

import pytest

import autosport.matchbook_api_cost_evidence as module
from autosport.matchbook_api_cost_evidence import (
    BillingCalendarBasis,
    CashTruth,
    FxTruth,
    MATCHBOOK_GET_BLOCK_PRICE_GBP,
    MATCHBOOK_GET_BLOCK_SIZE,
    MATCHBOOK_WRITE_QUALIFICATION,
    MatchbookApiCostEvidenceError,
    MatchbookApiRequestMeter,
    MatchbookApiUsageSnapshot,
    MatchbookBillingPeriod,
    MeterContinuityTruth,
    PolicyAmountTruth,
    UsageOriginTruth,
    calculate_policy_math,
    configured_billing_period,
    derive_matchbook_api_cost_accrual,
    public_matchbook_pricing_policy,
)


UTC = timezone.utc
START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)


def policy(
    digit: str = "a",
    observed: datetime = START - timedelta(days=1),
):
    return public_matchbook_pricing_policy(
        observed_at=observed,
        source_sha256=digit * 64,
    )


def period(digit: str = "b"):
    return configured_billing_period(
        period_id="matchbook-2026-09-config",
        starts_at=START,
        ends_at=END,
        timezone_name="CONFIGURED",
        config_sha256=digit * 64,
    )


def meter(*, p=None, b=None):
    return MatchbookApiRequestMeter(
        application_context_sha256="c" * 64,
        billing_period=period() if b is None else b,
        pricing_policy=policy() if p is None else p,
    )


def test_frozen_public_policy_constants_and_identity() -> None:
    p = policy()
    assert p.get_block_size == MATCHBOOK_GET_BLOCK_SIZE == 1_000_000
    assert (
        p.get_block_price_gbp
        == MATCHBOOK_GET_BLOCK_PRICE_GBP
        == Decimal("100")
    )
    assert p.write_qualification == MATCHBOOK_WRITE_QUALIFICATION
    assert p.policy_sha256 == policy().policy_sha256
    assert p.policy_sha256 != policy("f").policy_sha256


def test_policy_math_never_prorates_partial_million() -> None:
    p = policy()
    cases = (
        (999_999, 0, 999_999, None),
        (1_000_000, 1, 0, Decimal("100")),
        (1_000_001, 1, 1, None),
        (2_000_000, 2, 0, Decimal("200")),
    )
    for count, blocks, remainder, exact in cases:
        got = calculate_policy_math(count, p)
        assert got.completed_blocks == blocks
        assert got.remainder_requests == remainder
        assert got.exact_policy_gbp == exact
    for bad in (True, -1, 1.0, Decimal("1")):
        with pytest.raises(MatchbookApiCostEvidenceError):
            calculate_policy_math(bad, p)  # type: ignore[arg-type]


def test_billing_timezone_is_only_explicit_configured_assumption() -> None:
    b = period()
    assert b.basis is BillingCalendarBasis.CONFIGURED_ASSUMPTION
    direct = MatchbookBillingPeriod(
        period_id="still-explicit-config",
        starts_at=START,
        ends_at=END,
        timezone_name="UTC",
        config_sha256="d" * 64,
    )
    assert direct.basis is BillingCalendarBasis.CONFIGURED_ASSUMPTION
    assert not hasattr(module, "provider_documented_billing_period")


def test_pricing_policy_must_preexist_bound_period() -> None:
    with pytest.raises(MatchbookApiCostEvidenceError, match="no later"):
        meter(p=policy(observed=START + timedelta(microseconds=1)))


def test_meter_has_no_count_setter_consumer_or_write_dimension() -> None:
    assert "request_count" not in inspect.signature(
        MatchbookApiRequestMeter
    ).parameters
    params = inspect.signature(
        MatchbookApiRequestMeter.record_get_emitted
    ).parameters
    assert not {"request_count", "consumer", "strategy", "write"}.intersection(
        params
    )
    methods = {
        name
        for name, value in inspect.getmembers(MatchbookApiRequestMeter)
        if callable(value) and not name.startswith("_")
    }
    assert methods == {"record_get_emitted", "snapshot"}


def test_one_send_counts_once_and_retry_counts_twice() -> None:
    m = meter()
    fingerprint = "1" * 64
    m.record_get_emitted(
        sequence=1,
        request_fingerprint_sha256=fingerprint,
        emitted_at=START + timedelta(seconds=1),
    )
    first = m.rolling_emission_sha256
    m.record_get_emitted(
        sequence=2,
        request_fingerprint_sha256=fingerprint,
        emitted_at=START + timedelta(seconds=2),
    )
    snapshot = m.snapshot(observed_at=START + timedelta(seconds=3))
    assert snapshot.request_count == 2
    assert m.rolling_emission_sha256 != first
    assert snapshot.continuity_truth is MeterContinuityTruth.PROCESS_LOCAL_ONLY
    assert snapshot.origin_truth is UsageOriginTruth.UNBOUND_PROCESS_LOCAL


def test_fail_before_io_and_sequence_collision_do_not_mutate_meter() -> None:
    m = meter()
    before = m.rolling_emission_sha256
    with pytest.raises(MatchbookApiCostEvidenceError):
        m.record_get_emitted(
            sequence=1,
            request_fingerprint_sha256="bad",
            emitted_at=START,
        )
    assert (m.request_count, m.rolling_emission_sha256) == (0, before)

    m.record_get_emitted(
        sequence=1,
        request_fingerprint_sha256="2" * 64,
        emitted_at=START,
    )
    stable = m.rolling_emission_sha256
    for bad_sequence in (1, 3):
        with pytest.raises(MatchbookApiCostEvidenceError, match="expected 2"):
            m.record_get_emitted(
                sequence=bad_sequence,
                request_fingerprint_sha256="3" * 64,
                emitted_at=START,
            )
        assert (m.request_count, m.rolling_emission_sha256) == (1, stable)


def test_time_bounds_fail_closed_without_state_change() -> None:
    m = meter()
    with pytest.raises(MatchbookApiCostEvidenceError, match="outside"):
        m.record_get_emitted(
            sequence=1,
            request_fingerprint_sha256="4" * 64,
            emitted_at=END,
        )
    m.record_get_emitted(
        sequence=1,
        request_fingerprint_sha256="4" * 64,
        emitted_at=START + timedelta(seconds=2),
    )
    with pytest.raises(MatchbookApiCostEvidenceError, match="backwards"):
        m.record_get_emitted(
            sequence=2,
            request_fingerprint_sha256="5" * 64,
            emitted_at=START + timedelta(seconds=1),
        )
    with pytest.raises(MatchbookApiCostEvidenceError, match="predate"):
        m.snapshot(observed_at=START + timedelta(seconds=1))
    assert m.request_count == 1


def test_usage_identity_is_deterministic_and_capability_sealed() -> None:
    def build():
        m = meter()
        for sequence in (1, 2, 3):
            m.record_get_emitted(
                sequence=sequence,
                request_fingerprint_sha256=str(sequence) * 64,
                emitted_at=START + timedelta(seconds=sequence),
            )
        return m.snapshot(observed_at=START + timedelta(seconds=4))

    left = build()
    right = build()
    assert left == right
    assert left.evidence_sha256 == right.evidence_sha256
    with pytest.raises(MatchbookApiCostEvidenceError, match="capability"):
        replace(left, request_count=99, last_sequence=99)


def test_fabricated_snapshot_cannot_mint_evidence_without_meter_capability() -> None:
    legitimate = meter().snapshot(observed_at=START)
    assert not hasattr(module, "_TOKEN")
    assert not hasattr(module, "_usage_digest")

    with pytest.raises(MatchbookApiCostEvidenceError, match="capability"):
        MatchbookApiUsageSnapshot(
            application_context_sha256=legitimate.application_context_sha256,
            billing_period_sha256=legitimate.billing_period_sha256,
            pricing_policy_sha256=legitimate.pricing_policy_sha256,
            request_count=1_000_000,
            first_sequence=1,
            last_sequence=1_000_000,
            first_emitted_at=START,
            last_emitted_at=START,
            rolling_emission_sha256="d" * 64,
            observed_at=START,
            continuity_truth=MeterContinuityTruth.PROCESS_LOCAL_ONLY,
            evidence_sha256="e" * 64,
            _capability_proof=b"caller-cannot-sign-this",
        )


def test_post_construction_usage_mutation_is_revalidated_by_derivation() -> None:
    b = period()
    p = policy()
    usage = meter(b=b, p=p).snapshot(observed_at=START)
    object.__setattr__(usage, "request_count", 1_000_000)
    object.__setattr__(usage, "first_sequence", 1)
    object.__setattr__(usage, "last_sequence", 1_000_000)
    object.__setattr__(usage, "first_emitted_at", START)
    object.__setattr__(usage, "last_emitted_at", START)

    with pytest.raises(MatchbookApiCostEvidenceError, match="capability"):
        derive_matchbook_api_cost_accrual(
            usage=usage,
            billing_period=b,
            pricing_policy=p,
            account_currency="GBP",
        )


def test_remainder_usage_cannot_mint_prorated_gbp() -> None:
    m = meter()
    m.record_get_emitted(
        sequence=1,
        request_fingerprint_sha256="6" * 64,
        emitted_at=START,
    )
    accrual = derive_matchbook_api_cost_accrual(
        usage=m.snapshot(observed_at=START),
        billing_period=period(),
        pricing_policy=policy(),
        account_currency="GBP",
    )
    assert accrual.policy_gbp_amount is None
    assert accrual.amount_truth is PolicyAmountTruth.USAGE_ORIGIN_UNBOUND
    assert accrual.account_currency_amount is None
    assert accrual.usage_origin_truth is UsageOriginTruth.UNBOUND_PROCESS_LOCAL
    assert accrual.cash_truth is CashTruth.UNRECONCILED


def test_unbound_zero_usage_cannot_mint_account_wide_zero_cost() -> None:
    accrual = derive_matchbook_api_cost_accrual(
        usage=meter().snapshot(observed_at=START),
        billing_period=period(),
        pricing_policy=policy(),
        account_currency="GBP",
    )
    assert accrual.policy_gbp_amount is None
    assert accrual.amount_truth is PolicyAmountTruth.USAGE_ORIGIN_UNBOUND
    assert accrual.fx_truth is FxTruth.NOT_NEEDED_GBP
    assert accrual.account_currency_amount is None
    assert accrual.usage_origin_truth is UsageOriginTruth.UNBOUND_PROCESS_LOCAL
    assert accrual.cash_truth is CashTruth.UNRECONCILED
    assert accrual.allocation_state == "UNALLOCATED_SHARED_PROVIDER_COST"


def test_non_gbp_provider_fx_remains_unknown() -> None:
    accrual = derive_matchbook_api_cost_accrual(
        usage=meter().snapshot(observed_at=START),
        billing_period=period(),
        pricing_policy=policy(),
        account_currency="EUR",
    )
    assert accrual.fx_truth is FxTruth.UNRESOLVED_PROVIDER_FX
    assert accrual.account_currency_amount is None
    assert accrual.cash_truth is CashTruth.UNRECONCILED


def test_usage_cannot_be_rebound_to_other_period_or_policy() -> None:
    b = period()
    p = policy()
    usage = meter(b=b, p=p).snapshot(observed_at=START)
    with pytest.raises(MatchbookApiCostEvidenceError, match="billing period"):
        derive_matchbook_api_cost_accrual(
            usage=usage,
            billing_period=period("d"),
            pricing_policy=p,
            account_currency="GBP",
        )
    with pytest.raises(MatchbookApiCostEvidenceError, match="pricing policy"):
        derive_matchbook_api_cost_accrual(
            usage=usage,
            billing_period=b,
            pricing_policy=policy("e"),
            account_currency="GBP",
        )


def test_currency_and_accrual_tampering_fail_closed() -> None:
    usage = meter().snapshot(observed_at=START)
    for currency in ("gbp", "EURO", " EU"):
        with pytest.raises(MatchbookApiCostEvidenceError):
            derive_matchbook_api_cost_accrual(
                usage=usage,
                billing_period=period(),
                pricing_policy=policy(),
                account_currency=currency,
            )

    m = meter()
    m.record_get_emitted(
        sequence=1,
        request_fingerprint_sha256="7" * 64,
        emitted_at=START,
    )
    accrual = derive_matchbook_api_cost_accrual(
        usage=m.snapshot(observed_at=START),
        billing_period=period(),
        pricing_policy=policy(),
        account_currency="GBP",
    )
    with pytest.raises(MatchbookApiCostEvidenceError):
        replace(accrual, policy_gbp_amount=Decimal("0.0001"))
    with pytest.raises(MatchbookApiCostEvidenceError, match="capability"):
        replace(accrual, pricing_policy_sha256="f" * 64)
    with pytest.raises(MatchbookApiCostEvidenceError):
        replace(accrual, allocation_state="ALLOCATED")
    with pytest.raises(MatchbookApiCostEvidenceError):
        replace(accrual, completed_blocks=1, completed_block_gbp=Decimal("100"))

def test_caller_mutated_meter_state_cannot_mint_transport_observed_cost() -> None:
    b = period()
    p = policy()
    m = meter(b=b, p=p)

    m._count = 1_000_000
    m._first_seq = 1
    m._last_seq = 1_000_000
    m._first_at = START
    m._last_at = START
    m._rolling = "d" * 64

    usage = m.snapshot(observed_at=START)
    accrual = derive_matchbook_api_cost_accrual(
        usage=usage,
        billing_period=b,
        pricing_policy=p,
        account_currency="GBP",
    )

    assert usage.request_count == 1_000_000
    assert usage.origin_truth is UsageOriginTruth.UNBOUND_PROCESS_LOCAL
    assert (
        calculate_policy_math(usage.request_count, p).exact_policy_gbp
        == Decimal("100")
    )
    assert accrual.policy_gbp_amount is None
    assert accrual.account_currency_amount is None
    assert accrual.amount_truth is PolicyAmountTruth.USAGE_ORIGIN_UNBOUND

