from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import autosport.betfair_commission_cost_evidence as bridge
from autosport.betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairReadOnlyClient,
)
from autosport.betfair_market_commission_authority import (
    BetfairMarketCommissionAuthority,
    BetfairMarketCommissionAuthorityError,
    BetfairMarketCommissionReceipt,
)
from autosport.campaign_cost_evidence import (
    CostBasis,
    CostClass,
    CostTreatment,
    CostTruth,
    CostUnit,
)
from autosport.campaign_economic_authority import (
    CanonicalCampaignProjection,
    CanonicalMembershipRef,
    CanonicalSessionRef,
    FinalizedCampaignAuthority,
)
from autosport.campaign_provider_scope_authority import CampaignProviderScopeProjection


NOW = datetime(2026, 9, 20, 19, 0, tzinfo=timezone.utc)
CAMPAIGN_SHA = "1" * 64
SESSION_SHA = "2" * 64
RUN_SHA = "3" * 64
EVAL_SHA = "4" * 64
OTHER_SESSION_SHA = "5" * 64
OTHER_RUN_SHA = "6" * 64
OTHER_EVAL_SHA = "7" * 64
STABLE_ACCOUNT = "betfair-account-evidence:" + "8" * 64


def _projection() -> CanonicalCampaignProjection:
    memberships = tuple(
        sorted(
            (
                CanonicalMembershipRef("SESSION", "session-evidence-1", SESSION_SHA),
                CanonicalMembershipRef("RUN", "run-1", RUN_SHA),
                CanonicalMembershipRef("EVALUATION", "session-evidence-1", EVAL_SHA),
                CanonicalMembershipRef("SESSION", "session-evidence-2", OTHER_SESSION_SHA),
                CanonicalMembershipRef("RUN", "run-2", OTHER_RUN_SHA),
                CanonicalMembershipRef("EVALUATION", "session-evidence-2", OTHER_EVAL_SHA),
            )
        )
    )
    return CanonicalCampaignProjection(
        campaign_id="campaign-1",
        campaign_version=3,
        campaign_sha256=CAMPAIGN_SHA,
        session_refs=(
            CanonicalSessionRef("session-evidence-1", SESSION_SHA),
            CanonicalSessionRef("session-evidence-2", OTHER_SESSION_SHA),
        ),
        membership_refs=memberships,
        gross_run_pnl=Decimal("12.5"),
    )


def _scope(
    *,
    account_id: str = STABLE_ACCOUNT,
    market_id: str = "1.234",
    available_at: datetime | None = None,
) -> CampaignProviderScopeProjection:
    observed = NOW - timedelta(minutes=2)
    available = available_at or (NOW - timedelta(minutes=1))
    return CampaignProviderScopeProjection(
        campaign_id="campaign-1",
        campaign_version=3,
        campaign_sha256=CAMPAIGN_SHA,
        session_id="session-1",
        run_id="run-1",
        session_evidence_id="session-evidence-1",
        session_evidence_sha256=SESSION_SHA,
        run_summary_sha256=RUN_SHA,
        decision_id="decision-1",
        plan_id="plan-1",
        plan_fingerprint="plan-fingerprint",
        action_id="action-1",
        provider_capture_sha256="9" * 64,
        provider_evidence_id="provider-evidence-1",
        provider_source_sha256="a" * 64,
        venue_id="betfair",
        authenticated_account_id=account_id,
        event_id="event-1",
        market_id=market_id,
        source_interval_start=(NOW - timedelta(minutes=5)).isoformat(),
        source_interval_end=observed.isoformat(),
        observed_at=observed.isoformat(),
        available_at=available.isoformat(),
    )


def _receipt(
    *,
    market_id: str = "1.234",
    commission: Decimal = Decimal("2.25"),
    supersedes_receipt_id: str | None = None,
) -> BetfairMarketCommissionReceipt:
    return BetfairMarketCommissionReceipt(
        venue_id="betfair",
        account_id="betfair-account-evidence:" + "b" * 64,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        market_id=market_id,
        commission=commission,
        profit=Decimal("7.75"),
        currency="EUR",
        settled_at=NOW - timedelta(minutes=4),
        observed_at=NOW - timedelta(minutes=3),
        available_at=NOW - timedelta(minutes=3),
        account_details_sha256="b" * 64,
        cleared_orders_sha256="c" * 64,
        request_scope_sha256="d" * 64,
        supersedes_receipt_id=supersedes_receipt_id,
    )


def _authorities(
    monkeypatch: pytest.MonkeyPatch,
    *,
    receipt: BetfairMarketCommissionReceipt,
    stable_account_id: str = STABLE_ACCOUNT,
) -> tuple[BetfairMarketCommissionAuthority, FinalizedCampaignAuthority, BetfairReadOnlyClient]:
    source = object.__new__(BetfairMarketCommissionAuthority)
    source._client = object.__new__(BetfairReadOnlyClient)
    origin_client = object.__new__(BetfairReadOnlyClient)
    campaign = object.__new__(FinalizedCampaignAuthority)
    projection = _projection()

    monkeypatch.setattr(
        FinalizedCampaignAuthority,
        "projection",
        lambda self: projection,
    )
    monkeypatch.setattr(
        bridge._source_origin,
        "resolve_bound_receipt",
        lambda source, *, receipt_id, record_sha256, as_of: (receipt, origin_client),
    )
    monkeypatch.setattr(
        bridge._scope,
        "assert_campaign_provider_scope_authoritative",
        lambda value: None,
    )
    monkeypatch.setattr(
        bridge,
        "_stable_client_account_identity",
        lambda value: (stable_account_id, NOW - timedelta(seconds=30)),
    )
    return source, campaign, origin_client


def test_issue_binds_source_money_to_exact_campaign_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    provider_scope = _scope()

    evidence = bridge.issue_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=provider_scope,
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )

    assert evidence.cost_class is CostClass.EXECUTION_FEES_COMMISSION_TAX
    assert evidence.truth is CostTruth.KNOWN_AMOUNT
    assert evidence.basis is CostBasis.OBSERVED_INCURRED
    assert evidence.treatment is CostTreatment.INFORMATIONAL
    assert evidence.unit is CostUnit.MONEY
    assert evidence.amount == Decimal("2.25")
    assert evidence.currency == "EUR"
    assert evidence.incurred_at == receipt.settled_at
    assert evidence.available_at == NOW - timedelta(seconds=30)
    assert evidence.observed_at == NOW - timedelta(seconds=30)
    assert evidence.shared_source is True
    assert evidence.allocation_source is None
    assert evidence.source.evidence_id == receipt.receipt_id
    assert evidence.source.sha256 == receipt.record_sha256
    assert {(value.kind, value.evidence_id) for value in evidence.memberships} == {
        ("SESSION", "session-evidence-1"),
        ("RUN", "run-1"),
        ("EVALUATION", "session-evidence-1"),
    }
    assert all(value.evidence_id != "session-evidence-2" for value in evidence.memberships)


def test_zero_commission_is_authoritative_known_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt(commission=Decimal("0"))
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)

    evidence = bridge.issue_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=_scope(),
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )

    assert evidence.truth is CostTruth.KNOWN_ZERO
    assert evidence.amount == Decimal("0")
    assert evidence.basis is CostBasis.OBSERVED_INCURRED
    assert evidence.shared_source is True


def test_account_or_market_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(
        monkeypatch,
        receipt=receipt,
        stable_account_id="betfair-account-evidence:" + "e" * 64,
    )

    with pytest.raises(
        bridge.BetfairCommissionCostEvidenceError,
        match="receipt account",
    ):
        bridge.issue_betfair_commission_cost_evidence(
            source=source,
            campaign=campaign,
            provider_scope=_scope(),
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )

    monkeypatch.setattr(
        bridge,
        "_stable_client_account_identity",
        lambda value: (STABLE_ACCOUNT, NOW - timedelta(seconds=30)),
    )
    with pytest.raises(
        bridge.BetfairCommissionCostEvidenceError,
        match="commission market",
    ):
        bridge.issue_betfair_commission_cost_evidence(
            source=source,
            campaign=campaign,
            provider_scope=_scope(market_id="1.999"),
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )


def test_mutating_source_client_cannot_relabel_bound_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, origin_client = _authorities(monkeypatch, receipt=receipt)
    substituted_client = object.__new__(BetfairReadOnlyClient)
    source._client = substituted_client
    seen: list[BetfairReadOnlyClient] = []

    def stable_identity(client: BetfairReadOnlyClient):
        seen.append(client)
        return STABLE_ACCOUNT, NOW - timedelta(seconds=30)

    monkeypatch.setattr(bridge, "_stable_client_account_identity", stable_identity)
    scope_for_other_account = _scope(
        account_id="betfair-account-evidence:" + "e" * 64,
    )

    with pytest.raises(
        bridge.BetfairCommissionCostEvidenceError,
        match="receipt account",
    ):
        bridge.issue_betfair_commission_cost_evidence(
            source=source,
            campaign=campaign,
            provider_scope=scope_for_other_account,
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )

    assert seen == [origin_client]
    assert seen[0] is not substituted_client


def test_future_scope_account_identity_and_corrections_do_not_backfill_cost_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, origin_client = _authorities(monkeypatch, receipt=receipt)

    with pytest.raises(
        bridge.BetfairCommissionCostEvidenceError,
        match="future provider applicability",
    ):
        bridge.issue_betfair_commission_cost_evidence(
            source=source,
            campaign=campaign,
            provider_scope=_scope(available_at=NOW + timedelta(seconds=1)),
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )

    monkeypatch.setattr(
        bridge,
        "_stable_client_account_identity",
        lambda value: (STABLE_ACCOUNT, NOW + timedelta(seconds=1)),
    )
    with pytest.raises(
        bridge.BetfairCommissionCostEvidenceError,
        match="future account-identity verification",
    ):
        bridge.issue_betfair_commission_cost_evidence(
            source=source,
            campaign=campaign,
            provider_scope=_scope(),
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )

    monkeypatch.setattr(
        bridge,
        "_stable_client_account_identity",
        lambda value: (STABLE_ACCOUNT, NOW - timedelta(seconds=30)),
    )
    corrected = _receipt(supersedes_receipt_id="f" * 64)
    monkeypatch.setattr(
        bridge._source_origin,
        "resolve_bound_receipt",
        lambda source, *, receipt_id, record_sha256, as_of: (corrected, origin_client),
    )
    with pytest.raises(
        bridge.BetfairCommissionCostEvidenceError,
        match="append-only campaign cost correction lineage",
    ):
        bridge.issue_betfair_commission_cost_evidence(
            source=source,
            campaign=campaign,
            provider_scope=_scope(),
            receipt_id=corrected.receipt_id,
            record_sha256=corrected.record_sha256,
            as_of=NOW,
        )


def test_verifier_re_resolves_and_rejects_caller_amount_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    provider_scope = _scope()
    evidence = bridge.issue_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=provider_scope,
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )

    assert bridge.verify_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=provider_scope,
        evidence=evidence,
        as_of=NOW,
    )

    forged = replace(evidence, amount=Decimal("99"))
    assert not bridge.verify_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=provider_scope,
        evidence=forged,
        as_of=NOW,
    )


def test_forged_provider_scope_is_rejected_before_source_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source = object.__new__(BetfairMarketCommissionAuthority)
    source._client = object.__new__(BetfairReadOnlyClient)
    campaign = object.__new__(FinalizedCampaignAuthority)
    monkeypatch.setattr(FinalizedCampaignAuthority, "projection", lambda self: _projection())
    monkeypatch.setattr(
        bridge._scope,
        "assert_campaign_provider_scope_authoritative",
        lambda value: (_ for _ in ()).throw(RuntimeError("forged")),
    )
    called = False

    def should_not_resolve(source, *, receipt_id, record_sha256, as_of):
        nonlocal called
        called = True
        return receipt, object.__new__(BetfairReadOnlyClient)

    monkeypatch.setattr(bridge._source_origin, "resolve_bound_receipt", should_not_resolve)

    with pytest.raises(
        bridge.BetfairCommissionCostEvidenceError,
        match="not canonical campaign applicability authority",
    ):
        bridge.issue_betfair_commission_cost_evidence(
            source=source,
            campaign=campaign,
            provider_scope=_scope(),
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )
    assert called is False


def test_unregistered_source_cannot_resolve_receipt_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source = object.__new__(BetfairMarketCommissionAuthority)
    source._client = object.__new__(BetfairReadOnlyClient)
    monkeypatch.setattr(
        BetfairMarketCommissionAuthority,
        "resolve",
        lambda self, *, receipt_id, record_sha256, as_of: receipt,
    )

    with pytest.raises(
        BetfairMarketCommissionAuthorityError,
        match="lacks immutable current-process client-origin authority",
    ):
        bridge._source_origin.resolve_bound_receipt(
            source,
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )
