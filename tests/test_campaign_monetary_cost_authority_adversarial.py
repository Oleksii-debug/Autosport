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
    _admit_receipt,
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
        authority.store_receipt_candidate(simulated)
        with pytest.raises(MonetaryCostAuthorityError, match="non-incurred"):
            _admit_receipt(authority, simulated)
        with pytest.raises(MonetaryCostAuthorityIntegrityError, match="resolver admission"):
            authority.build_cost_evidence(
                receipt_id=simulated.receipt_id,
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
        _admit_receipt(authority, receipt)
        projection = campaign.projection()
        with pytest.raises(MonetaryCostAuthorityError, match="coverage"):
            authority.build_cost_evidence(
                receipt_id=receipt.receipt_id,
                campaign_sha256=projection.campaign_sha256,
                memberships=projection.membership_refs,
                required_interval=(T0, T2),
            )
        cost = authority.build_cost_evidence(
            receipt_id=receipt.receipt_id,
            campaign_sha256=projection.campaign_sha256,
            memberships=projection.membership_refs,
            required_interval=(T0, T1),
        )
        assert cost.amount == Decimal("2")
    finally:
        fixture.doCleanups()


def test_one_receipt_cannot_fork_into_two_append_only_corrections(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        projection = campaign.projection()
        first = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="2",
            suffix="invoice-base",
        )
        _admit_receipt(authority, first)
        correction = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="3",
            suffix="invoice-correction-a",
            supersedes=(first.receipt_id,),
        )
        _admit_receipt(authority, correction)
        competing = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="4",
            suffix="invoice-correction-b",
            supersedes=(first.receipt_id,),
        )
        with pytest.raises(MonetaryCostAuthorityError, match="different append-only correction"):
            _admit_receipt(authority, competing)
        with pytest.raises(MonetaryCostAuthorityIntegrityError, match="correction lineage"):
            authority.build_cost_evidence(
                receipt_id=competing.receipt_id,
                campaign_sha256=projection.campaign_sha256,
                memberships=projection.membership_refs,
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
            covered_start=T2,
            covered_end=T2,
        )
        _admit_receipt(authority, future)
        projection = campaign.projection()
        cost = authority.build_cost_evidence(
            receipt_id=future.receipt_id,
            campaign_sha256=projection.campaign_sha256,
            memberships=projection.membership_refs,
        )
        restarted = CampaignMonetaryCostAuthority(root)
        with pytest.raises(MonetaryCostAuthorityError, match="future-available"):
            restarted.qualify_cost(cost, as_of=T2)
    finally:
        fixture.doCleanups()


def test_candidate_tamper_or_source_collision_never_gains_admission(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        projection = campaign.projection()
        admitted = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="2",
            suffix="same-external-source",
        )
        _admit_receipt(authority, admitted)
        conflicting = replace(admitted, money=MoneyAmount(Decimal("3"), "EUR"))
        with pytest.raises(MonetaryCostAuthorityError, match="conflicting immutable evidence"):
            _admit_receipt(authority, conflicting)
        with pytest.raises(MonetaryCostAuthorityIntegrityError, match="resolver admission"):
            authority.build_cost_evidence(
                receipt_id=conflicting.receipt_id,
                campaign_sha256=projection.campaign_sha256,
                memberships=projection.membership_refs,
            )
    finally:
        fixture.doCleanups()
