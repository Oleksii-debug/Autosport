from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from autosport.campaign_authoritative_economics import (
    derive_authoritative_campaign_economics,
)
from autosport.campaign_cost_evidence import (
    CostSourceRef,
    EconomicCompleteness,
)
from autosport.monetary_cost_authority import (
    MonetaryCostAuthority,
    MonetaryEvidenceQuality,
    MonetarySourceClass,
    MonetarySourceSnapshot,
)
from test_campaign_cost_evidence import _fixture_authority


T0 = datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)


class Resolver:
    def __init__(self, snapshot: MonetarySourceSnapshot) -> None:
        self.snapshot = snapshot

    def resolve(self, locator: str, *, as_of: datetime):
        return self.snapshot if locator == self.snapshot.evidence_id else None


def _snapshot(
    campaign_id: str,
    *,
    evidence_id: str,
    amount: str,
    digit: str,
    currency: str = "EUR",
) -> MonetarySourceSnapshot:
    return MonetarySourceSnapshot(
        authority_id=f"authority-{evidence_id}",
        evidence_id=evidence_id,
        content_sha256=digit * 64,
        amount=Decimal(amount),
        currency=currency,
        campaign_ids=(campaign_id,),
        coverage_start=T0 - timedelta(hours=1),
        coverage_end=T0,
        observed_at=T0,
        available_at=T1,
        provenance=f"resolver:{evidence_id}",
        quality=MonetaryEvidenceQuality.INCURRED,
    )


def _build_authority(tmp_path, campaign, *, fixed_currency: str = "EUR"):
    campaign_id = campaign.projection().campaign_id
    specs = (
        (MonetarySourceClass.PROVIDER_BILLING, "provider-invoice", "2", "a", "EUR"),
        (MonetarySourceClass.COMPUTE_BILLING, "compute-invoice", "3", "b", "EUR"),
        (MonetarySourceClass.EXECUTION_SLIPPAGE, "slippage-receipt", "6", "c", "EUR"),
        (MonetarySourceClass.EXECUTION_RECEIPT, "execution-fee", "4", "d", "EUR"),
        (MonetarySourceClass.FIXED_ADMIN, "fixed-expense", "5", "e", fixed_currency),
    )
    snapshots = {
        source_class: _snapshot(
            campaign_id,
            evidence_id=evidence_id,
            amount=amount,
            digit=digit,
            currency=currency,
        )
        for source_class, evidence_id, amount, digit, currency in specs
    }
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={
            source_class: Resolver(snapshot)
            for source_class, snapshot in snapshots.items()
        },
    )
    costs = []
    for source_class, snapshot in snapshots.items():
        ref = authority.capture_source(
            source_class,
            snapshot.evidence_id,
            as_of=T1,
        )
        costs.append(
            authority.issue_cost_evidence(
                campaign=campaign,
                source_ref=ref,
                as_of=T1,
            )
        )
    return authority, tuple(costs)


def test_all_required_authoritative_costs_close_net_economics(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        monetary_authority, costs = _build_authority(tmp_path, campaign)
        version = derive_authoritative_campaign_economics(
            campaign=campaign,
            costs=costs,
            as_of=T1,
            monetary_authority=monetary_authority,
        )

        # Slippage is already embedded in campaign gross P&L. The other four
        # authoritative EUR sources are subtractive: 2 + 3 + 4 + 5 = 14.
        assert version.gross_run_pnl == Decimal("60")
        assert version.known_cost_total == Decimal("14")
        assert version.net_after_known_costs == Decimal("46")
        assert version.completeness is EconomicCompleteness.COMPLETE_NET_ECONOMICS
        assert version.incomplete_reasons == ()
    finally:
        fixture.doCleanups()


def test_mixed_authoritative_currencies_remain_fail_closed_without_fx(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        monetary_authority, costs = _build_authority(
            tmp_path,
            campaign,
            fixed_currency="USD",
        )
        version = derive_authoritative_campaign_economics(
            campaign=campaign,
            costs=costs,
            as_of=T1,
            monetary_authority=monetary_authority,
        )

        assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
        assert version.net_after_known_costs is None
        assert version.known_cost_total == Decimal("0")
        assert "CROSS_CURRENCY_REQUIRES_FX_AUTHORITY" in version.incomplete_reasons
    finally:
        fixture.doCleanups()


def test_one_forged_cost_keeps_class_unresolved_even_with_valid_receipt(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        monetary_authority, costs = _build_authority(tmp_path, campaign)
        provider = next(
            cost for cost in costs if cost.cost_class.value == "PROVIDER_DATA"
        )
        forged = replace(
            provider,
            source=CostSourceRef(
                family="candidate.external.cost",
                evidence_id="forged-provider-cost",
                sha256="f" * 64,
            ),
            amount=Decimal("999"),
        )
        version = derive_authoritative_campaign_economics(
            campaign=campaign,
            costs=costs + (forged,),
            as_of=T1,
            monetary_authority=monetary_authority,
        )

        assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
        assert version.net_after_known_costs is None
        assert "UNRESOLVED_COST_AUTHORITY:PROVIDER_DATA" in version.incomplete_reasons
        assert version.known_cost_total == Decimal("14")
    finally:
        fixture.doCleanups()
