from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
import pytest

from autosport.matchbook_api_cost_evidence import (
    BillingCalendarBasis, CashTruth, FxTruth, MATCHBOOK_GET_BLOCK_PRICE_GBP, MATCHBOOK_GET_BLOCK_SIZE,
    MATCHBOOK_WRITE_QUALIFICATION, MatchbookApiCostEvidenceError, MatchbookApiRequestMeter,
    MatchbookBillingPeriod, MeterContinuityTruth, PolicyAmountTruth, calculate_policy_math,
    configured_billing_period, derive_matchbook_api_cost_accrual, public_matchbook_pricing_policy,
)

UTC = timezone.utc
START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)


def policy(digit="a", observed=START - timedelta(days=1)):
    return public_matchbook_pricing_policy(observed_at=observed, source_sha256=digit * 64)


def period(digit="b"):
    return configured_billing_period(period_id="matchbook-2026-09-config", starts_at=START, ends_at=END,
                                     timezone_name="CONFIGURED", config_sha256=digit * 64)


def meter(*, p=None, b=None):
    return MatchbookApiRequestMeter(application_context_sha256="c" * 64,
        billing_period=period() if b is None else b, pricing_policy=policy() if p is None else p)


def test_frozen_public_policy_constants_and_identity():
    p = policy()
    assert p.get_block_size == MATCHBOOK_GET_BLOCK_SIZE == 1_000_000
    assert p.get_block_price_gbp == MATCHBOOK_GET_BLOCK_PRICE_GBP == Decimal("100")
    assert p.write_qualification == MATCHBOOK_WRITE_QUALIFICATION
    assert p.policy_sha256 == policy().policy_sha256 != policy("f").policy_sha256


def test_policy_math_never_prorates_partial_million():
    p = policy()
    cases = [(999_999, 0, 999_999, None), (1_000_000, 1, 0, Decimal("100")),
             (1_000_001, 1, 1, None), (2_000_000, 2, 0, Decimal("200"))]
    for count, blocks, rem, exact in cases:
        got = calculate_policy_math(count, p)
        assert (got.completed_blocks, got.remainder_requests, got.exact_policy_gbp) == (blocks, rem, exact)
    for bad in (True, -1, 1.0, Decimal("1")):
        with pytest.raises(MatchbookApiCostEvidenceError): calculate_policy_math(bad, p)  # type: ignore[arg-type]


def test_billing_timezone_is_only_explicit_configured_assumption():
    b = period()
    assert b.basis is BillingCalendarBasis.CONFIGURED_ASSUMPTION
    with pytest.raises(MatchbookApiCostEvidenceError):
        MatchbookBillingPeriod("x", START, END, "UTC", "d" * 64, _token=object())


def test_pricing_policy_must_preexist_bound_period():
    with pytest.raises(MatchbookApiCostEvidenceError, match="no later"):
        meter(p=policy(observed=START + timedelta(microseconds=1)))


def test_meter_has_no_count_setter_consumer_or_write_dimension():
    assert "request_count" not in inspect.signature(MatchbookApiRequestMeter).parameters
    params = inspect.signature(MatchbookApiRequestMeter.record_get_emitted).parameters
    assert not {"request_count", "consumer", "strategy", "write"}.intersection(params)
    methods = {n for n, v in inspect.getmembers(MatchbookApiRequestMeter) if callable(v) and not n.startswith("_")}
    assert methods == {"record_get_emitted", "snapshot"}


def test_one_send_counts_once_and_retry_counts_twice():
    m = meter(); fp = "1" * 64
    m.record_get_emitted(sequence=1, request_fingerprint_sha256=fp, emitted_at=START + timedelta(seconds=1))
    first = m.rolling_emission_sha256
    m.record_get_emitted(sequence=2, request_fingerprint_sha256=fp, emitted_at=START + timedelta(seconds=2))
    s = m.snapshot(observed_at=START + timedelta(seconds=3))
    assert s.request_count == 2 and m.rolling_emission_sha256 != first
    assert s.continuity_truth is MeterContinuityTruth.PROCESS_LOCAL_ONLY


def test_fail_before_io_and_sequence_collision_do_not_mutate_meter():
    m = meter(); before = m.rolling_emission_sha256
    with pytest.raises(MatchbookApiCostEvidenceError):
        m.record_get_emitted(sequence=1, request_fingerprint_sha256="bad", emitted_at=START)
    assert (m.request_count, m.rolling_emission_sha256) == (0, before)
    m.record_get_emitted(sequence=1, request_fingerprint_sha256="2" * 64, emitted_at=START)
    stable = m.rolling_emission_sha256
    for bad_seq in (1, 3):
        with pytest.raises(MatchbookApiCostEvidenceError, match="expected 2"):
            m.record_get_emitted(sequence=bad_seq, request_fingerprint_sha256="3" * 64, emitted_at=START)
        assert (m.request_count, m.rolling_emission_sha256) == (1, stable)


def test_time_bounds_fail_closed_without_state_change():
    m = meter()
    with pytest.raises(MatchbookApiCostEvidenceError, match="outside"):
        m.record_get_emitted(sequence=1, request_fingerprint_sha256="4" * 64, emitted_at=END)
    m.record_get_emitted(sequence=1, request_fingerprint_sha256="4" * 64, emitted_at=START + timedelta(seconds=2))
    with pytest.raises(MatchbookApiCostEvidenceError, match="backwards"):
        m.record_get_emitted(sequence=2, request_fingerprint_sha256="5" * 64, emitted_at=START + timedelta(seconds=1))
    with pytest.raises(MatchbookApiCostEvidenceError, match="predate"):
        m.snapshot(observed_at=START + timedelta(seconds=1))
    assert m.request_count == 1


def test_usage_identity_is_deterministic_and_tamper_evident():
    def build():
        m = meter()
        for i in (1, 2, 3): m.record_get_emitted(sequence=i, request_fingerprint_sha256=str(i) * 64, emitted_at=START + timedelta(seconds=i))
        return m.snapshot(observed_at=START + timedelta(seconds=4))
    left, right = build(), build()
    assert left == right and left.evidence_sha256 == right.evidence_sha256
    with pytest.raises(MatchbookApiCostEvidenceError): replace(left, request_count=99)


def test_remainder_usage_cannot_mint_prorated_gbp():
    m = meter(); m.record_get_emitted(sequence=1, request_fingerprint_sha256="6" * 64, emitted_at=START)
    a = derive_matchbook_api_cost_accrual(usage=m.snapshot(observed_at=START), billing_period=period(), pricing_policy=policy(), account_currency="GBP")
    assert a.policy_gbp_amount is None and a.amount_truth is PolicyAmountTruth.REMAINDER_UNRESOLVED
    assert a.account_currency_amount is None and a.cash_truth is CashTruth.UNRECONCILED


def test_whole_block_zero_is_configured_estimate_not_provider_cash_truth():
    a = derive_matchbook_api_cost_accrual(usage=meter().snapshot(observed_at=START), billing_period=period(), pricing_policy=policy(), account_currency="GBP")
    assert a.policy_gbp_amount == Decimal("0")
    assert a.amount_truth is PolicyAmountTruth.CONFIGURED_CALENDAR_ESTIMATE_GBP
    assert a.fx_truth is FxTruth.NOT_NEEDED_GBP and a.account_currency_amount == Decimal("0")
    assert a.cash_truth is CashTruth.UNRECONCILED and a.allocation_state == "UNALLOCATED_SHARED_PROVIDER_COST"


def test_non_gbp_provider_fx_remains_unknown():
    a = derive_matchbook_api_cost_accrual(usage=meter().snapshot(observed_at=START), billing_period=period(), pricing_policy=policy(), account_currency="EUR")
    assert a.fx_truth is FxTruth.UNRESOLVED_PROVIDER_FX and a.account_currency_amount is None
    assert a.cash_truth is CashTruth.UNRECONCILED


def test_usage_cannot_be_rebound_to_other_period_or_policy():
    b, p = period(), policy(); u = meter(b=b, p=p).snapshot(observed_at=START)
    with pytest.raises(MatchbookApiCostEvidenceError, match="billing period"):
        derive_matchbook_api_cost_accrual(usage=u, billing_period=period("d"), pricing_policy=p, account_currency="GBP")
    with pytest.raises(MatchbookApiCostEvidenceError, match="pricing policy"):
        derive_matchbook_api_cost_accrual(usage=u, billing_period=b, pricing_policy=policy("e"), account_currency="GBP")


def test_currency_and_accrual_tampering_fail_closed():
    u = meter().snapshot(observed_at=START)
    for currency in ("gbp", "EURO", " EU"):
        with pytest.raises(MatchbookApiCostEvidenceError):
            derive_matchbook_api_cost_accrual(usage=u, billing_period=period(), pricing_policy=policy(), account_currency=currency)
    m = meter(); m.record_get_emitted(sequence=1, request_fingerprint_sha256="7" * 64, emitted_at=START)
    a = derive_matchbook_api_cost_accrual(usage=m.snapshot(observed_at=START), billing_period=period(), pricing_policy=policy(), account_currency="GBP")
    with pytest.raises(MatchbookApiCostEvidenceError): replace(a, policy_gbp_amount=Decimal("0.0001"))
    with pytest.raises(MatchbookApiCostEvidenceError): replace(a, allocation_state="ALLOCATED")
