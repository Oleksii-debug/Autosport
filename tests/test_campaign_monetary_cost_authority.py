from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib

import pytest

from autosport.campaign_authoritative_economics import (
    derive_authoritative_campaign_economics,
)
from autosport.campaign_cost_evidence import (
    CostBasis,
    CostClass,
    CostEvidence,
    CostSourceRef,
    CostTreatment,
    CostTruth,
    CostUnit,
    EconomicCompleteness,
)
from autosport.campaign_economic_authority import (
    CanonicalMembershipRef,
    FinalizedCampaignAuthority,
)
from autosport.campaign_evidence import CampaignOutcome, CampaignReadiness
from autosport.campaign_monetary_cost_authority import (
    AllocationPlan,
    AllocationTarget,
    CampaignCurrencyEvidence,
    CampaignMonetaryCostAuthority,
    MoneyAmount,
    MonetaryCostAuthorityError,
    MonetaryCostAuthorityIntegrityError,
    MonetaryReceipt,
    MonetarySourceClass,
)
from test_campaign_evidence import PaperCampaignTests


T0 = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fixture_authority() -> tuple[PaperCampaignTests, FinalizedCampaignAuthority]:
    fixture = PaperCampaignTests(
        methodName="test_session_evidence_hash_binds_window_and_metrics"
    )
    fixture.setUp()
    campaign = fixture.campaign()
    fixture.add_session(campaign, fixture.session())
    campaign.finalize(
        outcome=CampaignOutcome.POSITIVE,
        readiness=CampaignReadiness.ELIGIBLE,
        finalized_at="2026-09-11T00:00:00Z",
    )
    return fixture, FinalizedCampaignAuthority(campaign)


def _source_class(cost_class: CostClass) -> MonetarySourceClass:
    if cost_class is CostClass.PROVIDER_DATA:
        return MonetarySourceClass.PROVIDER_BILLING
    if cost_class is CostClass.MODEL_COMPUTE_AI:
        return MonetarySourceClass.COMPUTE_BILLING
    if cost_class in {
        CostClass.EXECUTION_SLIPPAGE,
        CostClass.EXECUTION_FEES_COMMISSION_TAX,
    }:
        return MonetarySourceClass.EXECUTION_RECEIPT
    return MonetarySourceClass.FIXED_CAMPAIGN_ADMIN


def _receipt(
    campaign: FinalizedCampaignAuthority,
    *,
    cost_class: CostClass,
    amount: str,
    suffix: str,
    currency: str = "EUR",
    shared: bool = False,
    supersedes: tuple[str, ...] = (),
) -> MonetaryReceipt:
    projection = campaign.projection()
    return MonetaryReceipt(
        cost_class=cost_class,
        source_class=_source_class(cost_class),
        source_authority=f"authority.{cost_class.value.lower()}",
        source_evidence_id=f"receipt-{suffix}",
        source_sha256=_sha(f"source-{suffix}"),
        provenance_sha256=_sha(f"provenance-{suffix}"),
        money=MoneyAmount(Decimal(amount), currency),
        incurred_start=T0,
        incurred_end=T0,
        observed_at=T0,
        available_at=T1,
        treatment=CostTreatment.SUBTRACT_FROM_GROSS,
        shared_source=shared,
        campaign_sha256=None if shared else projection.campaign_sha256,
        memberships=() if shared else projection.membership_refs,
        supersedes_receipt_ids=tuple(sorted(supersedes)),
    )


def _currency(
    campaign: FinalizedCampaignAuthority,
    *,
    currency: str = "EUR",
    suffix: str = "eur",
) -> CampaignCurrencyEvidence:
    return CampaignCurrencyEvidence(
        campaign_sha256=campaign.projection().campaign_sha256,
        currency=currency,
        source_authority="campaign.bankroll.currency",
        source_evidence_id=f"currency-{suffix}",
        source_sha256=_sha(f"currency-source-{suffix}"),
        observed_at=T0,
        available_at=T1,
    )


def test_authoritative_receipts_can_close_complete_net_economics(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        currency_ref = authority.publish_currency(_currency(campaign))
        costs = []
        for index, cost_class in enumerate(CostClass, start=1):
            receipt = _receipt(
                campaign,
                cost_class=cost_class,
                amount=str(index),
                suffix=f"complete-{index}",
            )
            authority.publish_receipt(receipt)
            costs.append(
                authority.build_cost_evidence(
                    receipt_id=receipt.receipt_id,
                    campaign_sha256=campaign.projection().campaign_sha256,
                    memberships=campaign.projection().membership_refs,
                )
            )

        version = derive_authoritative_campaign_economics(
            campaign=campaign,
            costs=tuple(sorted(costs, key=lambda item: item.cost_evidence_id)),
            as_of=T2,
            monetary_authority=authority,
            currency_ref=currency_ref,
        )

        assert version.gross_run_pnl == Decimal("60")
        assert version.known_cost_total == Decimal("15")
        assert version.net_after_known_costs == Decimal("45")
        assert version.completeness is EconomicCompleteness.COMPLETE_NET_ECONOMICS
        assert version.incomplete_reasons == ()
    finally:
        fixture.doCleanups()


def test_forged_caller_cost_and_public_price_cannot_upgrade_to_incurred_truth(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        currency_ref = authority.publish_currency(_currency(campaign))
        projection = campaign.projection()
        forged = CostEvidence(
            cost_class=CostClass.PROVIDER_DATA,
            truth=CostTruth.KNOWN_AMOUNT,
            basis=CostBasis.CONFIGURED_ESTIMATE,
            treatment=CostTreatment.SUBTRACT_FROM_GROSS,
            source=CostSourceRef(
                family="public.provider.price.page",
                evidence_id="marketing-price",
                sha256=_sha("public-price"),
            ),
            campaign_sha256=projection.campaign_sha256,
            memberships=projection.membership_refs,
            unit=CostUnit.MONEY,
            currency="EUR",
            amount=Decimal("99"),
            observed_at=T0,
            available_at=T1,
            incurred_at=None,
        )

        version = derive_authoritative_campaign_economics(
            campaign=campaign,
            costs=(forged,),
            as_of=T2,
            monetary_authority=authority,
            currency_ref=currency_ref,
        )
        assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
        assert version.known_cost_total == Decimal("0")
        assert "UNRESOLVED_COST_AUTHORITY:PROVIDER_DATA" in version.incomplete_reasons
        assert "ESTIMATE_ONLY:PROVIDER_DATA" in version.incomplete_reasons
    finally:
        fixture.doCleanups()


def test_cross_currency_cost_stays_fail_closed_without_fx_authority(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        currency_ref = authority.publish_currency(_currency(campaign, currency="EUR"))
        costs = []
        for index, cost_class in enumerate(CostClass, start=1):
            receipt = _receipt(
                campaign,
                cost_class=cost_class,
                amount="1",
                suffix=f"currency-{index}",
                currency="USD" if cost_class is CostClass.MODEL_COMPUTE_AI else "EUR",
            )
            authority.publish_receipt(receipt)
            costs.append(
                authority.build_cost_evidence(
                    receipt_id=receipt.receipt_id,
                    campaign_sha256=campaign.projection().campaign_sha256,
                    memberships=campaign.projection().membership_refs,
                )
            )

        version = derive_authoritative_campaign_economics(
            campaign=campaign,
            costs=tuple(sorted(costs, key=lambda item: item.cost_evidence_id)),
            as_of=T2,
            monetary_authority=authority,
            currency_ref=currency_ref,
        )
        assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
        assert "CURRENCY_MISMATCH:MODEL_COMPUTE_AI" in version.incomplete_reasons
        assert version.known_cost_total == Decimal("4")
        assert version.net_after_known_costs == Decimal("56")
    finally:
        fixture.doCleanups()


def test_shared_cost_requires_one_conserving_allocation_plan(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        receipt = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="10",
            suffix="shared-provider",
            shared=True,
        )
        authority.publish_receipt(receipt)
        projection = campaign.projection()
        other_membership = (
            CanonicalMembershipRef("RUN", "other-run", _sha("other-run")),
        )
        targets = tuple(
            sorted(
                (
                    AllocationTarget(
                        projection.campaign_sha256,
                        projection.membership_refs,
                        MoneyAmount(Decimal("4"), "EUR"),
                    ),
                    AllocationTarget(
                        "f" * 64,
                        other_membership,
                        MoneyAmount(Decimal("6"), "EUR"),
                    ),
                ),
                key=lambda value: value.target_key,
            )
        )
        bad_targets = tuple(
            replace(item, money=MoneyAmount(Decimal("5"), "EUR"))
            if item.campaign_sha256 == "f" * 64
            else item
            for item in targets
        )
        bad_plan = AllocationPlan(
            receipt_id=receipt.receipt_id,
            receipt_sha256=receipt.record_sha256,
            targets=tuple(sorted(bad_targets, key=lambda value: value.target_key)),
            observed_at=T1,
            available_at=T1,
        )
        with pytest.raises(MonetaryCostAuthorityError, match="conserve"):
            authority.publish_allocation(bad_plan)

        plan = AllocationPlan(
            receipt_id=receipt.receipt_id,
            receipt_sha256=receipt.record_sha256,
            targets=targets,
            observed_at=T1,
            available_at=T1,
        )
        authority.publish_allocation(plan)
        cost = authority.build_cost_evidence(
            receipt_id=receipt.receipt_id,
            campaign_sha256=projection.campaign_sha256,
            memberships=projection.membership_refs,
        )
        assert cost.amount == Decimal("4")
        assert cost.shared_source is True
        assert cost.allocation_source == plan.ref
    finally:
        fixture.doCleanups()


def test_duplicate_external_receipt_identity_with_conflicting_amount_is_rejected(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        first = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="2",
            suffix="invoice-42",
        )
        authority.publish_receipt(first)
        conflicting = replace(first, money=MoneyAmount(Decimal("3"), "EUR"))
        with pytest.raises(MonetaryCostAuthorityError, match="conflicting immutable evidence"):
            authority.publish_receipt(conflicting)
    finally:
        fixture.doCleanups()


def test_receipt_correction_is_append_only_and_projects_cost_supersession(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        first = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="2",
            suffix="invoice-v1",
        )
        authority.publish_receipt(first)
        first_cost = authority.build_cost_evidence(
            receipt_id=first.receipt_id,
            campaign_sha256=campaign.projection().campaign_sha256,
            memberships=campaign.projection().membership_refs,
        )
        corrected = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="3",
            suffix="invoice-v2",
            supersedes=(first.receipt_id,),
        )
        authority.publish_receipt(corrected)
        corrected_cost = authority.build_cost_evidence(
            receipt_id=corrected.receipt_id,
            campaign_sha256=campaign.projection().campaign_sha256,
            memberships=campaign.projection().membership_refs,
        )
        assert corrected_cost.supersedes_cost_evidence_ids == (
            first_cost.cost_evidence_id,
        )
        assert (authority.receipts_dir / f"{first.receipt_id}.json").exists()
        assert (authority.receipts_dir / f"{corrected.receipt_id}.json").exists()
    finally:
        fixture.doCleanups()


def test_restart_readback_detects_receipt_tamper(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        root = tmp_path / "money"
        authority = CampaignMonetaryCostAuthority(root)
        receipt = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="2",
            suffix="tamper",
        )
        authority.publish_receipt(receipt)
        cost = authority.build_cost_evidence(
            receipt_id=receipt.receipt_id,
            campaign_sha256=campaign.projection().campaign_sha256,
            memberships=campaign.projection().membership_refs,
        )
        restarted = CampaignMonetaryCostAuthority(root)
        assert restarted.qualify_cost(cost, as_of=T2) == cost

        path = restarted.receipts_dir / f"{receipt.receipt_id}.json"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"amount":"2"', '"amount":"3"'), encoding="utf-8")
        with pytest.raises(MonetaryCostAuthorityIntegrityError):
            restarted.qualify_cost(cost, as_of=T2)
    finally:
        fixture.doCleanups()


def test_money_rejects_negative_nonfinite_and_source_class_mismatch() -> None:
    with pytest.raises(MonetaryCostAuthorityError):
        MoneyAmount(Decimal("-1"), "EUR")
    with pytest.raises(MonetaryCostAuthorityError):
        MoneyAmount(Decimal("NaN"), "EUR")

    fixture, campaign = _fixture_authority()
    try:
        valid = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="1",
            suffix="wrong-source",
        )
        with pytest.raises(MonetaryCostAuthorityError, match="cannot authorize"):
            replace(valid, source_class=MonetarySourceClass.EXECUTION_RECEIPT)
    finally:
        fixture.doCleanups()
