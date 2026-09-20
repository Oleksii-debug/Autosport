from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from autosport.campaign_cost_evidence import CostClass, CostTreatment
from autosport.campaign_monetary_cost_authority import (
    CampaignMonetaryCostAuthority,
    MonetaryCostAuthorityError,
    MonetaryCostAuthorityIntegrityError,
    MonetaryEvidenceQuality,
    MoneyAmount,
)
from test_campaign_monetary_cost_authority import (
    T0,
    T1,
    T2,
    _fixture_authority,
    _receipt,
)


def test_simulated_or_estimated_record_never_mints_incurred_cost(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        simulated = replace(
            _receipt(
                campaign,
                cost_class=CostClass.EXECUTION_FEES_COMMISSION_TAX,
                amount="4",
                suffix="paper-fee-simulation",
            ),
            quality=MonetaryEvidenceQuality.SIMULATED,
            treatment=CostTreatment.INFORMATIONAL,
        )
        authority.publish_receipt(simulated)
        with pytest.raises(MonetaryCostAuthorityError, match="cannot mint incurred"):
            authority.build_cost_evidence(
                receipt_id=simulated.receipt_id,
                campaign_sha256=campaign.projection().campaign_sha256,
                memberships=campaign.projection().membership_refs,
            )
    finally:
        fixture.doCleanups()


def test_conflicting_unbound_orphan_receipt_cannot_qualify(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        first = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="2",
            suffix="same-external-id",
        )
        authority.publish_receipt(first)
        conflicting = replace(first, money=MoneyAmount(Decimal("3"), "EUR"))
        with pytest.raises(MonetaryCostAuthorityError, match="conflicting immutable evidence"):
            authority.publish_receipt(conflicting)
        assert (authority.receipts_dir / f"{conflicting.receipt_id}.json").exists()
        with pytest.raises(MonetaryCostAuthorityIntegrityError, match="source identity binding"):
            authority.build_cost_evidence(
                receipt_id=conflicting.receipt_id,
                campaign_sha256=campaign.projection().campaign_sha256,
                memberships=campaign.projection().membership_refs,
            )
    finally:
        fixture.doCleanups()


def test_receipt_coverage_interval_fails_closed_when_campaign_interval_is_not_covered(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        receipt = replace(
            _receipt(
                campaign,
                cost_class=CostClass.PROVIDER_DATA,
                amount="2",
                suffix="narrow-coverage",
            ),
            covered_start=T0,
            covered_end=T1,
        )
        authority.publish_receipt(receipt)
        with pytest.raises(MonetaryCostAuthorityError, match="coverage"):
            authority.build_cost_evidence(
                receipt_id=receipt.receipt_id,
                campaign_sha256=campaign.projection().campaign_sha256,
                memberships=campaign.projection().membership_refs,
                required_interval=(T0, T2),
            )
        cost = authority.build_cost_evidence(
            receipt_id=receipt.receipt_id,
            campaign_sha256=campaign.projection().campaign_sha256,
            memberships=campaign.projection().membership_refs,
            required_interval=(T0, T1),
        )
        assert cost.amount == Decimal("2")
    finally:
        fixture.doCleanups()


def test_one_receipt_cannot_fork_into_two_append_only_corrections(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        first = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="2",
            suffix="invoice-base",
        )
        authority.publish_receipt(first)
        correction = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="3",
            suffix="invoice-correction-a",
            supersedes=(first.receipt_id,),
        )
        authority.publish_receipt(correction)
        competing = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="4",
            suffix="invoice-correction-b",
            supersedes=(first.receipt_id,),
        )
        with pytest.raises(MonetaryCostAuthorityError, match="different append-only correction"):
            authority.publish_receipt(competing)
        with pytest.raises(MonetaryCostAuthorityIntegrityError, match="correction lineage"):
            authority.build_cost_evidence(
                receipt_id=competing.receipt_id,
                campaign_sha256=campaign.projection().campaign_sha256,
                memberships=campaign.projection().membership_refs,
            )
    finally:
        fixture.doCleanups()


def test_future_available_cost_remains_unusable_after_restart(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        root = tmp_path / "money"
        authority = CampaignMonetaryCostAuthority(root)
        future = replace(
            _receipt(
                campaign,
                cost_class=CostClass.FIXED_CAMPAIGN,
                amount="1",
                suffix="future-admin-expense",
            ),
            observed_at=T2,
            available_at=T2 + timedelta(minutes=1),
            incurred_end=T2,
        )
        authority.publish_receipt(future)
        cost = authority.build_cost_evidence(
            receipt_id=future.receipt_id,
            campaign_sha256=campaign.projection().campaign_sha256,
            memberships=campaign.projection().membership_refs,
        )
        restarted = CampaignMonetaryCostAuthority(root)
        with pytest.raises(MonetaryCostAuthorityError, match="future-available"):
            restarted.qualify_cost(cost, as_of=T2)
    finally:
        fixture.doCleanups()
