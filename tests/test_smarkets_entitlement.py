from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.smarkets_entitlement import (
    SmarketsApprovalState,
    SmarketsEntitlementError,
    SmarketsEntitlementObservation,
    SmarketsPurpose,
    SmarketsSetupCostObservation,
    evaluate_smarkets_entitlement,
    is_product_issued_smarkets_setup_cost,
    issue_smarkets_entitlement_observation,
    issue_smarkets_setup_cost_observation,
    resolve_smarkets_entitlement,
)


UTC = timezone.utc
DOC_SHA = "a" * 64
NOW = datetime(2026, 9, 22, 18, 0, tzinfo=UTC)


def _entitlement(
    *,
    state: SmarketsApprovalState = SmarketsApprovalState.APPROVED,
    observed_at: datetime = NOW,
    purposes: tuple[SmarketsPurpose, ...] = (
        SmarketsPurpose.ACCOUNT_RECONCILIATION,
        SmarketsPurpose.ORDER_EXECUTION,
    ),
    all_events: bool = False,
    events: tuple[str, ...] = ("event-1",),
    all_markets: bool = False,
    markets: tuple[str, ...] = ("market-1",),
):
    return issue_smarkets_entitlement_observation(
        account_scope="acct-scope-1",
        approval_ref="provider-approval-2026-09",
        approval_state=state,
        approved_purposes=purposes,
        all_events_approved=all_events,
        allowed_event_ids=events,
        all_markets_approved=all_markets,
        allowed_market_ids=markets,
        rate_policy_ref="provider-rate-policy-1",
        max_requests_per_window=120,
        rate_window_seconds=60,
        observed_at=observed_at,
        source_document_sha256=DOC_SHA,
    )


def _evaluate(observation, **overrides):
    values = {
        "account_scope": "acct-scope-1",
        "purpose": SmarketsPurpose.ORDER_EXECUTION,
        "event_id": "event-1",
        "market_id": "market-1",
        "as_of": NOW + timedelta(seconds=10),
        "max_age_seconds": 300,
    }
    values.update(overrides)
    return evaluate_smarkets_entitlement(observation, **values)


def test_exact_fresh_scope_is_entitled_but_never_execution_authorized():
    result = _evaluate(_entitlement())
    assert result.entitled is True
    assert result.reason == "ENTITLEMENT_EVIDENCE_SATISFIED"
    assert result.execution_authorized is False
    assert result.rate_policy_ref == "provider-rate-policy-1"
    assert result.max_requests_per_window == 120
    assert result.rate_window_seconds == 60


def test_http_reachability_is_not_an_entitlement_input():
    observation = _entitlement()
    assert not hasattr(observation, "http_status")
    assert not hasattr(observation, "reachable")
    assert not hasattr(observation, "session_token")


def test_unapproved_purpose_fails_closed():
    result = _evaluate(
        _entitlement(),
        purpose=SmarketsPurpose.REDISTRIBUTION,
    )
    assert result.entitled is False
    assert result.reason == "PURPOSE_NOT_APPROVED"


def test_provider_prohibited_benchmarking_cannot_be_minted():
    with pytest.raises(SmarketsEntitlementError, match="provider terms prohibit"):
        _entitlement(purposes=(SmarketsPurpose.BENCHMARKING,))


def test_account_scope_mismatch_fails_closed_and_decision_binds_source_scope():
    result = _evaluate(_entitlement(), account_scope="other-account")
    assert result.entitled is False
    assert result.reason == "ACCOUNT_SCOPE_MISMATCH"
    assert result.account_scope == "acct-scope-1"


def test_event_scope_mismatch_fails_closed():
    result = _evaluate(_entitlement(), event_id="event-2")
    assert result.entitled is False
    assert result.reason == "EVENT_NOT_APPROVED"


def test_market_scope_mismatch_fails_closed():
    result = _evaluate(_entitlement(), market_id="market-2")
    assert result.entitled is False
    assert result.reason == "MARKET_NOT_APPROVED"


def test_explicit_all_scope_is_supported_without_fake_wildcard_ids():
    observation = _entitlement(
        all_events=True,
        events=(),
        all_markets=True,
        markets=(),
    )
    result = _evaluate(
        observation,
        event_id="any-event",
        market_id="any-market",
    )
    assert result.entitled is True


def test_bounded_scope_cannot_be_empty():
    with pytest.raises(SmarketsEntitlementError, match="event scope"):
        _entitlement(events=())
    with pytest.raises(SmarketsEntitlementError, match="market scope"):
        _entitlement(markets=())


def test_all_scope_cannot_mix_with_explicit_ids():
    with pytest.raises(SmarketsEntitlementError, match="all-events"):
        _entitlement(all_events=True, events=("event-1",))
    with pytest.raises(SmarketsEntitlementError, match="all-markets"):
        _entitlement(all_markets=True, markets=("market-1",))


def test_suspended_and_revoked_observations_remove_positive_entitlement():
    suspended = _evaluate(
        _entitlement(state=SmarketsApprovalState.SUSPENDED)
    )
    revoked = _evaluate(
        _entitlement(state=SmarketsApprovalState.REVOKED)
    )
    assert suspended.entitled is False
    assert suspended.reason == "APPROVAL_SUSPENDED"
    assert revoked.entitled is False
    assert revoked.reason == "APPROVAL_REVOKED"


def test_stale_and_future_evidence_fail_closed():
    stale = _evaluate(
        _entitlement(observed_at=NOW - timedelta(hours=1)),
        as_of=NOW,
        max_age_seconds=300,
    )
    future = _evaluate(
        _entitlement(observed_at=NOW + timedelta(seconds=1)),
        as_of=NOW,
    )
    assert stale.reason == "STALE_EVIDENCE"
    assert future.reason == "FUTURE_EVIDENCE"


def test_direct_constructor_cannot_mint_positive_entitlement():
    issued = _entitlement()
    forged = SmarketsEntitlementObservation(
        account_scope=issued.account_scope,
        approval_ref=issued.approval_ref,
        approval_state=issued.approval_state,
        approved_purposes=issued.approved_purposes,
        all_events_approved=issued.all_events_approved,
        allowed_event_ids=issued.allowed_event_ids,
        all_markets_approved=issued.all_markets_approved,
        allowed_market_ids=issued.allowed_market_ids,
        rate_policy_ref=issued.rate_policy_ref,
        max_requests_per_window=issued.max_requests_per_window,
        rate_window_seconds=issued.rate_window_seconds,
        observed_at=issued.observed_at,
        source_document_sha256=issued.source_document_sha256,
        evidence_sha256=issued.evidence_sha256,
    )
    result = _evaluate(forged)
    assert result.entitled is False
    assert result.reason == "UNISSUED_OR_MUTATED_EVIDENCE"


def test_dataclasses_replace_does_not_inherit_issuance():
    issued = _entitlement()
    copied = replace(issued)
    result = _evaluate(copied)
    assert result.entitled is False
    assert result.reason == "UNISSUED_OR_MUTATED_EVIDENCE"


def test_in_place_mutation_revokes_issuance_even_on_same_object():
    issued = _entitlement()
    object.__setattr__(issued, "approval_ref", "tampered")
    result = _evaluate(issued)
    assert result.entitled is False
    assert result.reason == "UNISSUED_OR_MUTATED_EVIDENCE"


def test_entitlement_scope_and_purposes_must_be_canonical_not_silently_normalized():
    with pytest.raises(SmarketsEntitlementError, match="approved_purposes must be sorted and unique"):
        _entitlement(purposes=(SmarketsPurpose.ORDER_EXECUTION, SmarketsPurpose.ACCOUNT_RECONCILIATION))
    with pytest.raises(SmarketsEntitlementError, match="allowed_event_ids must be sorted and unique"):
        _entitlement(events=("event-2", "event-1"))
    with pytest.raises(SmarketsEntitlementError, match="allowed_market_ids must be sorted and unique"):
        _entitlement(markets=("market-1", "market-1"))


def test_rate_policy_must_be_explicit_and_positive():
    with pytest.raises(SmarketsEntitlementError, match="rate_policy_ref"):
        issue_smarkets_entitlement_observation(
            account_scope="acct",
            approval_ref="approval",
            approval_state=SmarketsApprovalState.APPROVED,
            approved_purposes=(SmarketsPurpose.ORDER_EXECUTION,),
            all_events_approved=True,
            allowed_event_ids=(),
            all_markets_approved=True,
            allowed_market_ids=(),
            rate_policy_ref="",
            max_requests_per_window=1,
            rate_window_seconds=1,
            observed_at=NOW,
            source_document_sha256=DOC_SHA,
        )
    with pytest.raises(SmarketsEntitlementError, match="positive integer"):
        issue_smarkets_entitlement_observation(
            account_scope="acct",
            approval_ref="approval",
            approval_state=SmarketsApprovalState.APPROVED,
            approved_purposes=(SmarketsPurpose.ORDER_EXECUTION,),
            all_events_approved=True,
            allowed_event_ids=(),
            all_markets_approved=True,
            allowed_market_ids=(),
            rate_policy_ref="policy",
            max_requests_per_window=0,
            rate_window_seconds=1,
            observed_at=NOW,
            source_document_sha256=DOC_SHA,
        )


def test_purpose_dimensions_do_not_collapse():
    observation = _entitlement(
        purposes=(
            SmarketsPurpose.MARKET_DATA_FOR_TRADING,
            SmarketsPurpose.ORDER_EXECUTION,
        )
    )
    assert _evaluate(
        observation, purpose=SmarketsPurpose.MARKET_DATA_FOR_TRADING
    ).entitled
    assert not _evaluate(
        observation, purpose=SmarketsPurpose.RESEARCH
    ).entitled
    assert not _evaluate(
        observation, purpose=SmarketsPurpose.REDISTRIBUTION
    ).entitled


def test_later_revocation_controls_later_as_of_without_rewriting_history():
    approved = _entitlement(observed_at=NOW)
    revoked = _entitlement(
        state=SmarketsApprovalState.REVOKED,
        observed_at=NOW + timedelta(minutes=1),
    )
    before = resolve_smarkets_entitlement(
        (approved, revoked),
        account_scope="acct-scope-1",
        purpose=SmarketsPurpose.ORDER_EXECUTION,
        event_id="event-1",
        market_id="market-1",
        as_of=NOW + timedelta(seconds=30),
        max_age_seconds=300,
    )
    after = resolve_smarkets_entitlement(
        (approved, revoked),
        account_scope="acct-scope-1",
        purpose=SmarketsPurpose.ORDER_EXECUTION,
        event_id="event-1",
        market_id="market-1",
        as_of=NOW + timedelta(minutes=2),
        max_age_seconds=300,
    )
    assert before.entitled is True
    assert after.entitled is False
    assert after.reason == "APPROVAL_REVOKED"


def test_same_time_conflicting_observations_fail_closed():
    approved = _entitlement(observed_at=NOW)
    revoked = _entitlement(
        state=SmarketsApprovalState.REVOKED,
        observed_at=NOW,
    )
    with pytest.raises(SmarketsEntitlementError, match="conflicting"):
        resolve_smarkets_entitlement(
            (approved, revoked),
            account_scope="acct-scope-1",
            purpose=SmarketsPurpose.ORDER_EXECUTION,
            event_id="event-1",
            market_id="market-1",
            as_of=NOW + timedelta(seconds=1),
            max_age_seconds=300,
        )


def test_setup_cost_is_one_time_not_per_bet_and_not_incurred_truth():
    observation = issue_smarkets_setup_cost_observation(
        amount=Decimal("150"),
        currency="GBP",
        billing_trigger="successful_api_activation",
        observed_at=NOW,
        source_document_sha256=DOC_SHA,
        refund_window_days=60,
        refund_condition="permanent_api_withdrawal_within_window",
    )
    assert observation.refund_window_days == 60
    assert observation.refund_condition == "permanent_api_withdrawal_within_window"
    assert observation.one_time is True
    assert observation.applies_per_bet is False
    assert observation.incurred_cost_authority is False
    assert is_product_issued_smarkets_setup_cost(observation) is True


def test_setup_cost_amount_is_not_hard_coded():
    observation = issue_smarkets_setup_cost_observation(
        amount=Decimal("175"),
        currency="GBP",
        billing_trigger="successful_api_activation",
        observed_at=NOW,
        source_document_sha256=DOC_SHA,
    )
    assert observation.amount == Decimal("175")


def test_setup_cost_direct_constructor_does_not_mint_source_issuance():
    issued = issue_smarkets_setup_cost_observation(
        amount=Decimal("150"),
        currency="GBP",
        billing_trigger="successful_api_activation",
        observed_at=NOW,
        source_document_sha256=DOC_SHA,
    )
    forged = SmarketsSetupCostObservation(
        amount=issued.amount,
        currency=issued.currency,
        billing_trigger=issued.billing_trigger,
        observed_at=issued.observed_at,
        source_document_sha256=issued.source_document_sha256,
        evidence_sha256=issued.evidence_sha256,
    )
    assert is_product_issued_smarkets_setup_cost(forged) is False


def test_setup_cost_copy_and_mutation_lose_product_issuance():
    issued = issue_smarkets_setup_cost_observation(
        amount=Decimal("150"),
        currency="GBP",
        billing_trigger="successful_api_activation",
        observed_at=NOW,
        source_document_sha256=DOC_SHA,
    )
    copied = replace(issued)
    assert is_product_issued_smarkets_setup_cost(copied) is False
    object.__setattr__(issued, "amount", Decimal("151"))
    assert is_product_issued_smarkets_setup_cost(issued) is False


def test_issue_rejects_noncanonical_collection_types_and_purpose_values():
    with pytest.raises(SmarketsEntitlementError, match="approved_purposes"):
        issue_smarkets_entitlement_observation(
            account_scope="acct",
            approval_ref="approval",
            approval_state=SmarketsApprovalState.APPROVED,
            approved_purposes=("order_execution",),
            all_events_approved=True,
            allowed_event_ids=(),
            all_markets_approved=True,
            allowed_market_ids=(),
            rate_policy_ref="policy",
            max_requests_per_window=1,
            rate_window_seconds=1,
            observed_at=NOW,
            source_document_sha256=DOC_SHA,
        )
    with pytest.raises(SmarketsEntitlementError, match="allowed_event_ids"):
        issue_smarkets_entitlement_observation(
            account_scope="acct",
            approval_ref="approval",
            approval_state=SmarketsApprovalState.APPROVED,
            approved_purposes=(SmarketsPurpose.ORDER_EXECUTION,),
            all_events_approved=False,
            allowed_event_ids=["event-1"],
            all_markets_approved=True,
            allowed_market_ids=(),
            rate_policy_ref="policy",
            max_requests_per_window=1,
            rate_window_seconds=1,
            observed_at=NOW,
            source_document_sha256=DOC_SHA,
        )


def test_setup_cost_refund_terms_are_paired():
    with pytest.raises(SmarketsEntitlementError, match="supplied together"):
        issue_smarkets_setup_cost_observation(
            amount=Decimal("150"),
            currency="GBP",
            billing_trigger="successful_api_activation",
            observed_at=NOW,
            source_document_sha256=DOC_SHA,
            refund_window_days=60,
        )


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_setup_cost_requires_finite_positive_amount(amount):
    with pytest.raises(SmarketsEntitlementError, match="positive Decimal"):
        issue_smarkets_setup_cost_observation(
            amount=amount,
            currency="GBP",
            billing_trigger="successful_api_activation",
            observed_at=NOW,
            source_document_sha256=DOC_SHA,
        )


def test_setup_cost_currency_is_explicit_and_canonical():
    with pytest.raises(SmarketsEntitlementError, match="currency"):
        issue_smarkets_setup_cost_observation(
            amount=Decimal("150"),
            currency="gbp",
            billing_trigger="successful_api_activation",
            observed_at=NOW,
            source_document_sha256=DOC_SHA,
        )


def test_source_digest_is_required_not_a_url_or_reachability_flag():
    with pytest.raises(SmarketsEntitlementError, match="SHA-256"):
        issue_smarkets_setup_cost_observation(
            amount=Decimal("150"),
            currency="GBP",
            billing_trigger="successful_api_activation",
            observed_at=NOW,
            source_document_sha256="https://docs.smarkets.com/",
        )
