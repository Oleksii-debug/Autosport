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
    MonetaryEvidenceQuality,
    MonetaryReceipt,
    MonetaryResolverFamily,
    MonetarySourceClass,
    ResolvedCampaignCurrency,
    ResolvedMonetaryReceipt,
    _issue_resolved_currency,
    _issue_resolved_receipt,
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


def _resolver_family(cost_class: CostClass) -> MonetaryResolverFamily:
    if cost_class is CostClass.PROVIDER_DATA:
        return MonetaryResolverFamily.PROVIDER_ACCOUNT_BILLING
    if cost_class is CostClass.MODEL_COMPUTE_AI:
        return MonetaryResolverFamily.COMPUTE_BILLING
    if cost_class in {
        CostClass.EXECUTION_SLIPPAGE,
        CostClass.EXECUTION_FEES_COMMISSION_TAX,
    }:
        return MonetaryResolverFamily.EXECUTION_SETTLEMENT
    return MonetaryResolverFamily.OWNER_FIXED_EXPENSE


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


def _resolved_receipt(receipt: MonetaryReceipt) -> ResolvedMonetaryReceipt:
    """Simulate a separately reviewed package-owned native source resolver in tests."""

    return _issue_resolved_receipt(
        receipt=receipt,
        resolver_family=_resolver_family(receipt.cost_class),
        resolver_evidence_id=f"native-resolution:{receipt.source_evidence_id}",
        resolver_sha256=_sha(f"native-resolution:{receipt.receipt_id}"),
    )


def _resolved_currency(evidence: CampaignCurrencyEvidence) -> ResolvedCampaignCurrency:
    """Simulate a separately reviewed owner/account currency resolver in tests."""

    return _issue_resolved_currency(
        evidence=evidence,
        resolver_evidence_id=f"owner-currency:{evidence.source_evidence_id}",
        resolver_sha256=_sha(f"owner-currency:{evidence.evidence_id}"),
    )


def _admit_receipt(
    authority: CampaignMonetaryCostAuthority, receipt: MonetaryReceipt
) -> CostSourceRef:
    return authority.publish_receipt(_resolved_receipt(receipt))


def _admit_currency(
    authority: CampaignMonetaryCostAuthority, evidence: CampaignCurrencyEvidence
) -> CostSourceRef:
    return authority.publish_currency(_resolved_currency(evidence))


def test_raw_self_consistent_forged_receipt_and_currency_cannot_cross_admission_boundary(
    tmp_path,
) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        projection = campaign.projection()
        forged_receipt = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="999",
            suffix="caller-forged-invoice",
        )
        forged_currency = _currency(campaign, suffix="caller-forged-currency")

        # Raw bytes may be retained as candidates for audit, but candidate storage
        # is intentionally not a positive monetary authority.
        authority.store_receipt_candidate(forged_receipt)
        authority.store_currency_candidate(forged_currency)

        with pytest.raises(MonetaryCostAuthorityError, match="ResolvedMonetaryReceipt"):
            authority.publish_receipt(forged_receipt)  # type: ignore[arg-type]
        with pytest.raises(MonetaryCostAuthorityError, match="ResolvedCampaignCurrency"):
            authority.publish_currency(forged_currency)  # type: ignore[arg-type]

        # Opaque admission capabilities cannot be constructed through their public
        # constructors even when every forged field/digest is internally consistent.
        with pytest.raises(TypeError, match="cannot be caller-constructed"):
            ResolvedMonetaryReceipt(
                forged_receipt,
                MonetaryResolverFamily.PROVIDER_ACCOUNT_BILLING,
                "caller-resolution",
                _sha("caller-resolution"),
            )
        with pytest.raises(TypeError, match="cannot be caller-constructed"):
            ResolvedCampaignCurrency(
                forged_currency,
                MonetaryResolverFamily.OWNER_CAMPAIGN_CURRENCY,
                "caller-currency-resolution",
                _sha("caller-currency-resolution"),
            )

        with pytest.raises(MonetaryCostAuthorityIntegrityError, match="resolver admission"):
            authority.build_cost_evidence(
                receipt_id=forged_receipt.receipt_id,
                campaign_sha256=projection.campaign_sha256,
                memberships=projection.membership_refs,
            )
        with pytest.raises(MonetaryCostAuthorityIntegrityError, match="resolver admission"):
            authority.resolve_campaign_currency(
                forged_currency.ref,
                campaign_sha256=projection.campaign_sha256,
                as_of=T2,
            )

        forged_cost = CostEvidence(
            cost_class=CostClass.PROVIDER_DATA,
            truth=CostTruth.KNOWN_AMOUNT,
            basis=CostBasis.OBSERVED_INCURRED,
            treatment=CostTreatment.SUBTRACT_FROM_GROSS,
            source=forged_receipt.ref,
            campaign_sha256=projection.campaign_sha256,
            memberships=projection.membership_refs,
            unit=CostUnit.MONEY,
            currency="EUR",
            amount=Decimal("999"),
            observed_at=T0,
            available_at=T1,
            incurred_at=T0,
        )
        version = derive_authoritative_campaign_economics(
            campaign=campaign,
            costs=(forged_cost,),
            as_of=T2,
            monetary_authority=authority,
            currency_ref=None,
        )
        assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
        assert version.known_cost_total == Decimal("0")
        assert version.net_after_known_costs is None
        assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" in version.incomplete_reasons
        assert "UNRESOLVED_COST_AUTHORITY:PROVIDER_DATA" in version.incomplete_reasons
    finally:
        fixture.doCleanups()


def test_package_owned_resolver_capabilities_can_drive_complete_economics_plumbing(
    tmp_path,
) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        currency_ref = _admit_currency(authority, _currency(campaign))
        costs: list[CostEvidence] = []
        projection = campaign.projection()
        for index, cost_class in enumerate(CostClass, start=1):
            receipt = _receipt(
                campaign,
                cost_class=cost_class,
                amount=str(index),
                suffix=f"resolved-{index}",
            )
            _admit_receipt(authority, receipt)
            costs.append(
                authority.build_cost_evidence(
                    receipt_id=receipt.receipt_id,
                    campaign_sha256=projection.campaign_sha256,
                    memberships=projection.membership_refs,
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


def test_public_price_or_configured_estimate_stays_non_incurred(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        currency_ref = _admit_currency(authority, _currency(campaign))
        projection = campaign.projection()
        public_price = CostEvidence(
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
            costs=(public_price,),
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
        currency_ref = _admit_currency(authority, _currency(campaign, currency="EUR"))
        costs: list[CostEvidence] = []
        projection = campaign.projection()
        for index, cost_class in enumerate(CostClass, start=1):
            receipt = _receipt(
                campaign,
                cost_class=cost_class,
                amount="1",
                suffix=f"currency-{index}",
                currency="USD" if cost_class is CostClass.MODEL_COMPUTE_AI else "EUR",
            )
            _admit_receipt(authority, receipt)
            costs.append(
                authority.build_cost_evidence(
                    receipt_id=receipt.receipt_id,
                    campaign_sha256=projection.campaign_sha256,
                    memberships=projection.membership_refs,
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


def test_shared_cost_requires_admitted_receipt_and_one_conserving_allocation(tmp_path) -> None:
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
        _admit_receipt(authority, receipt)
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
        bad = AllocationPlan(
            receipt_id=receipt.receipt_id,
            receipt_sha256=receipt.record_sha256,
            targets=tuple(
                sorted(
                    (
                        targets[0],
                        replace(targets[1], money=MoneyAmount(Decimal("5"), "EUR")),
                    ),
                    key=lambda value: value.target_key,
                )
            ),
            observed_at=T1,
            available_at=T1,
        )
        with pytest.raises(MonetaryCostAuthorityError, match="conserve"):
            authority.publish_allocation(bad)

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


def test_duplicate_external_source_identity_with_conflicting_amount_is_rejected(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        first = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="2",
            suffix="invoice-42",
        )
        _admit_receipt(authority, first)
        conflicting = replace(first, money=MoneyAmount(Decimal("3"), "EUR"))
        with pytest.raises(MonetaryCostAuthorityError, match="conflicting immutable evidence"):
            _admit_receipt(authority, conflicting)
        with pytest.raises(MonetaryCostAuthorityIntegrityError, match="resolver admission"):
            authority.build_cost_evidence(
                receipt_id=conflicting.receipt_id,
                campaign_sha256=campaign.projection().campaign_sha256,
                memberships=campaign.projection().membership_refs,
            )
    finally:
        fixture.doCleanups()


def test_correction_is_append_only_and_projects_exact_cost_supersession(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = CampaignMonetaryCostAuthority(tmp_path / "money")
        projection = campaign.projection()
        first = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="2",
            suffix="invoice-v1",
        )
        _admit_receipt(authority, first)
        first_cost = authority.build_cost_evidence(
            receipt_id=first.receipt_id,
            campaign_sha256=projection.campaign_sha256,
            memberships=projection.membership_refs,
        )
        corrected = _receipt(
            campaign,
            cost_class=CostClass.PROVIDER_DATA,
            amount="3",
            suffix="invoice-v2",
            supersedes=(first.receipt_id,),
        )
        _admit_receipt(authority, corrected)
        corrected_cost = authority.build_cost_evidence(
            receipt_id=corrected.receipt_id,
            campaign_sha256=projection.campaign_sha256,
            memberships=projection.membership_refs,
        )
        assert corrected_cost.supersedes_cost_evidence_ids == (first_cost.cost_evidence_id,)
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
        _admit_receipt(authority, receipt)
        projection = campaign.projection()
        cost = authority.build_cost_evidence(
            receipt_id=receipt.receipt_id,
            campaign_sha256=projection.campaign_sha256,
            memberships=projection.membership_refs,
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


def test_money_and_source_family_validation_fail_closed() -> None:
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
        with pytest.raises(MonetaryCostAuthorityError, match="cannot resolve"):
            _issue_resolved_receipt(
                receipt=valid,
                resolver_family=MonetaryResolverFamily.EXECUTION_SETTLEMENT,
                resolver_evidence_id="wrong-family",
                resolver_sha256=_sha("wrong-family"),
            )
    finally:
        fixture.doCleanups()
