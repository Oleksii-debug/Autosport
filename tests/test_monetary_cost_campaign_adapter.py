from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.campaign_cost_evidence import CostEvidenceError, CostTreatment
from autosport.monetary_cost_authority import (
    CampaignMonetaryAllocation,
    IncurredMonetaryReceipt,
    MonetaryCostAuthorityStore,
    MonetaryEvidenceQuality,
    MonetarySourceClass,
)
from autosport.monetary_cost_campaign_adapter import resolve_campaign_monetary_cost
from test_campaign_cost_evidence import _fixture_authority


UTC = timezone.utc


def _t(hour: int) -> datetime:
    return datetime(2026, 9, 20, hour, 0, tzinfo=UTC)


def _sha(char: str) -> str:
    return char * 64


def test_campaign_cost_amount_and_currency_are_reresolved_from_allocation(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        projection = campaign.projection()
        authority = MonetaryCostAuthorityStore(
            tmp_path / "workspace",
            authority_root=tmp_path / "authority",
        )
        receipt = IncurredMonetaryReceipt(
            source_class=MonetarySourceClass.PROVIDER_DATA,
            source_family="provider.billing.invoice",
            source_authority_id="provider-account-7",
            source_evidence_id="invoice-2026-09",
            source_sha256=_sha("a"),
            amount=Decimal("12.50"),
            currency="EUR",
            incurred_from=_t(6),
            incurred_to=_t(7),
            observed_at=_t(7),
            available_at=_t(7),
            quality=MonetaryEvidenceQuality.INCURRED_RECEIPT,
        )
        authority.append_receipt(receipt)
        allocation = CampaignMonetaryAllocation(
            receipt_id=receipt.receipt_id,
            receipt_sha256=receipt.record_sha256,
            campaign_sha256=projection.campaign_sha256,
            amount=Decimal("4.25"),
            currency="EUR",
            allocated_at=_t(8),
        )
        authority.append_allocation(allocation)

        cost = resolve_campaign_monetary_cost(
            authority=authority,
            campaign=campaign,
            allocation_id=allocation.allocation_id,
            allocation_sha256=allocation.record_sha256,
            memberships=projection.membership_refs,
            treatment=CostTreatment.SUBTRACT_FROM_GROSS,
            as_of=_t(9),
        )
        assert cost.amount == Decimal("4.25")
        assert cost.currency == "EUR"
        assert cost.source.evidence_id == allocation.allocation_id
        assert cost.campaign_sha256 == projection.campaign_sha256
    finally:
        fixture.doCleanups()


def test_wrong_allocation_digest_cannot_mint_observed_cost(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        projection = campaign.projection()
        authority = MonetaryCostAuthorityStore(
            tmp_path / "workspace",
            authority_root=tmp_path / "authority",
        )
        receipt = IncurredMonetaryReceipt(
            source_class=MonetarySourceClass.FIXED_CAMPAIGN,
            source_family="owner.fixed-expense",
            source_authority_id="owner-admin-ledger",
            source_evidence_id="expense-1",
            source_sha256=_sha("b"),
            amount=Decimal("2"),
            currency="EUR",
            incurred_from=_t(6),
            incurred_to=_t(6),
            observed_at=_t(6),
            available_at=_t(6),
            quality=MonetaryEvidenceQuality.OWNER_FIXED_EXPENSE,
        )
        authority.append_receipt(receipt)
        allocation = CampaignMonetaryAllocation(
            receipt.receipt_id,
            receipt.record_sha256,
            projection.campaign_sha256,
            Decimal("2"),
            "EUR",
            _t(7),
        )
        authority.append_allocation(allocation)
        with pytest.raises(CostEvidenceError, match="canonical resolution"):
            resolve_campaign_monetary_cost(
                authority=authority,
                campaign=campaign,
                allocation_id=allocation.allocation_id,
                allocation_sha256=_sha("f"),
                memberships=projection.membership_refs,
                treatment=CostTreatment.SUBTRACT_FROM_GROSS,
                as_of=_t(8),
            )
    finally:
        fixture.doCleanups()
