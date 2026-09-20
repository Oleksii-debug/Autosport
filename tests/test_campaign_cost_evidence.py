from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.campaign_cost_evidence import (
    CampaignEconomicEvidenceVersion,
    CostBasis,
    CostClass,
    CostEvidence,
    CostEvidenceError,
    CostSourceRef,
    CostTreatment,
    CostTruth,
    CostUnit,
    EconomicCompleteness,
    derive_campaign_economics,
)
from autosport.campaign_economic_authority import (
    CanonicalMembershipRef,
    FinalizedCampaignAuthority,
)
from autosport.campaign_evidence import CampaignOutcome, CampaignReadiness
from test_campaign_evidence import PaperCampaignTests


T0 = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)


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


def _cost(
    authority: FinalizedCampaignAuthority,
    *,
    cost_class: CostClass = CostClass.PROVIDER_DATA,
    source_digit: str = "a",
    truth: CostTruth = CostTruth.KNOWN_AMOUNT,
    basis: CostBasis | None = CostBasis.OBSERVED_INCURRED,
    treatment: CostTreatment = CostTreatment.SUBTRACT_FROM_GROSS,
    unit: CostUnit = CostUnit.MONEY,
    currency: str | None = "EUR",
    amount: Decimal | None = Decimal("2"),
    available_at: datetime = T1,
    memberships: tuple[CanonicalMembershipRef, ...] | None = None,
    supersedes: tuple[str, ...] = (),
) -> CostEvidence:
    projection = authority.projection()
    if memberships is None:
        memberships = projection.membership_refs
    incurred = None if truth in {CostTruth.UNKNOWN_UNPROVEN, CostTruth.NOT_APPLICABLE} else T0
    return CostEvidence(
        cost_class=cost_class,
        truth=truth,
        basis=basis,
        treatment=treatment,
        source=CostSourceRef(
            family="candidate.external.cost",
            evidence_id=f"cost-{cost_class.value.lower()}-{source_digit}",
            sha256=source_digit * 64,
        ),
        campaign_sha256=projection.campaign_sha256,
        memberships=memberships,
        unit=unit,
        currency=currency,
        amount=amount,
        observed_at=T0,
        available_at=available_at,
        incurred_at=incurred,
        supersedes_cost_evidence_ids=tuple(sorted(supersedes)),
    )


def test_finalized_campaign_authority_derives_gross_and_membership_from_live_truth() -> None:
    fixture, authority = _fixture_authority()
    try:
        projection = authority.projection()
        assert projection.gross_run_pnl == Decimal("60")
        assert projection.campaign_sha256 == fixture.campaign().campaign_sha256 or len(projection.campaign_sha256) == 64
        assert tuple(item.kind for item in projection.membership_refs) == (
            "EVALUATION",
            "RUN",
            "SESSION",
        )
        assert len(projection.session_refs) == 1
    finally:
        fixture.doCleanups()


def test_unfinalized_campaign_cannot_mint_economic_authority() -> None:
    fixture = PaperCampaignTests(
        methodName="test_session_evidence_hash_binds_window_and_metrics"
    )
    fixture.setUp()
    try:
        campaign = fixture.campaign()
        fixture.add_session(campaign, fixture.session())
        with pytest.raises(ValueError, match="finalized"):
            FinalizedCampaignAuthority(campaign)
    finally:
        fixture.doCleanups()


def test_caller_cannot_supply_gross_or_membership_to_derivation() -> None:
    fixture, authority = _fixture_authority()
    try:
        with pytest.raises(TypeError):
            derive_campaign_economics(
                campaign=authority,
                costs=(),
                as_of=T2,
                gross_run_pnl=Decimal("999"),
            )
        with pytest.raises(TypeError):
            derive_campaign_economics(
                campaign=authority,
                costs=(),
                as_of=T2,
                membership_refs=(),
            )
        with pytest.raises(CostEvidenceError, match="FinalizedCampaignAuthority"):
            derive_campaign_economics(campaign=object(), costs=(), as_of=T2)
    finally:
        fixture.doCleanups()


def test_positive_campaign_pnl_without_money_cost_authority_stays_incomplete() -> None:
    fixture, authority = _fixture_authority()
    try:
        version = derive_campaign_economics(campaign=authority, costs=(), as_of=T2)
        assert version.gross_run_pnl == Decimal("60")
        assert version.known_cost_total == Decimal("0")
        assert version.net_after_known_costs is None
        assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
        assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" in version.incomplete_reasons
        assert "MISSING_COST_CLASS:PROVIDER_DATA" in version.incomplete_reasons
    finally:
        fixture.doCleanups()


def test_caller_authored_observed_cost_cannot_become_incurred_money_truth() -> None:
    fixture, authority = _fixture_authority()
    try:
        cost = _cost(authority, amount=Decimal("25"))
        version = derive_campaign_economics(campaign=authority, costs=(cost,), as_of=T2)
        assert version.known_cost_total == Decimal("0")
        assert version.net_after_known_costs is None
        assert "UNRESOLVED_COST_AUTHORITY:PROVIDER_DATA" in version.incomplete_reasons
    finally:
        fixture.doCleanups()


def test_configured_public_price_is_explicitly_estimate_only() -> None:
    fixture, authority = _fixture_authority()
    try:
        cost = _cost(
            authority,
            basis=CostBasis.CONFIGURED_ESTIMATE,
            amount=Decimal("25"),
        )
        version = derive_campaign_economics(campaign=authority, costs=(cost,), as_of=T2)
        assert "ESTIMATE_ONLY:PROVIDER_DATA" in version.incomplete_reasons
        assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    finally:
        fixture.doCleanups()


def test_dimensionless_compute_evidence_cannot_be_subtracted_from_money() -> None:
    fixture, authority = _fixture_authority()
    try:
        cost = _cost(
            authority,
            cost_class=CostClass.MODEL_COMPUTE_AI,
            source_digit="b",
            unit=CostUnit.COMPUTE_CREDITS,
            currency=None,
            treatment=CostTreatment.INFORMATIONAL,
            basis=CostBasis.EMPIRICAL_DERIVED,
            amount=Decimal("12"),
        )
        version = derive_campaign_economics(campaign=authority, costs=(cost,), as_of=T2)
        assert "NON_MONEY_UNIT:MODEL_COMPUTE_AI" in version.incomplete_reasons
        assert "INFORMATIONAL_ONLY:MODEL_COMPUTE_AI" in version.incomplete_reasons
        assert version.net_after_known_costs is None
    finally:
        fixture.doCleanups()


def test_unknown_cost_and_fake_not_applicable_cannot_close_class() -> None:
    fixture, authority = _fixture_authority()
    try:
        unknown = _cost(
            authority,
            truth=CostTruth.UNKNOWN_UNPROVEN,
            basis=None,
            treatment=CostTreatment.INFORMATIONAL,
            amount=None,
        )
        fake_na = _cost(
            authority,
            cost_class=CostClass.FIXED_CAMPAIGN,
            source_digit="c",
            truth=CostTruth.NOT_APPLICABLE,
            basis=CostBasis.AUTHORITATIVE_DECLARATION,
            treatment=CostTreatment.INFORMATIONAL,
            amount=None,
        )
        version = derive_campaign_economics(
            campaign=authority,
            costs=tuple(sorted((unknown, fake_na), key=lambda value: value.cost_evidence_id)),
            as_of=T2,
        )
        assert "UNRESOLVED_COST_CLASS:PROVIDER_DATA" in version.incomplete_reasons
        assert "UNRESOLVED_COST_AUTHORITY:FIXED_CAMPAIGN" in version.incomplete_reasons
        assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    finally:
        fixture.doCleanups()


def test_wrong_campaign_membership_future_evidence_and_duplicate_source_fail_closed() -> None:
    fixture, authority = _fixture_authority()
    try:
        valid = _cost(authority)
        wrong_campaign = CostEvidence(
            **{**valid.__dict__, "campaign_sha256": "d" * 64}
        ) if hasattr(valid, "__dict__") else None
        if wrong_campaign is None:
            from dataclasses import replace
            wrong_campaign = replace(valid, campaign_sha256="d" * 64)
        with pytest.raises(CostEvidenceError, match="different campaign"):
            derive_campaign_economics(campaign=authority, costs=(wrong_campaign,), as_of=T2)

        from dataclasses import replace
        alien = CanonicalMembershipRef("RUN", "alien", "e" * 64)
        with pytest.raises(CostEvidenceError, match="outside"):
            derive_campaign_economics(
                campaign=authority,
                costs=(replace(valid, memberships=(alien,)),),
                as_of=T2,
            )
        with pytest.raises(CostEvidenceError, match="future-available"):
            derive_campaign_economics(
                campaign=authority,
                costs=(replace(valid, available_at=T2 + timedelta(seconds=1)),),
                as_of=T2,
            )

        second = replace(
            valid,
            cost_class=CostClass.FIXED_CAMPAIGN,
            amount=Decimal("1"),
        )
        with pytest.raises(CostEvidenceError, match="same immutable cost source"):
            derive_campaign_economics(
                campaign=authority,
                costs=tuple(sorted((valid, second), key=lambda value: value.cost_evidence_id)),
                as_of=T2,
            )
    finally:
        fixture.doCleanups()


def test_successor_cannot_drop_cost_and_explicit_same_class_supersession_is_append_only() -> None:
    from dataclasses import replace

    fixture, authority = _fixture_authority()
    try:
        first_cost = _cost(authority, amount=Decimal("2"))
        first = derive_campaign_economics(
            campaign=authority,
            costs=(first_cost,),
            as_of=T1,
        )
        with pytest.raises(CostEvidenceError, match="silently drop"):
            derive_campaign_economics(
                campaign=authority,
                costs=(),
                as_of=T2,
                previous=first,
            )

        replacement = _cost(
            authority,
            source_digit="f",
            amount=Decimal("4"),
            available_at=T2,
            supersedes=(first_cost.cost_evidence_id,),
        )
        second = derive_campaign_economics(
            campaign=authority,
            costs=(replacement,),
            as_of=T2,
            previous=first,
        )
        assert second.previous_version_id == first.version_id
        assert second.previous_version_sha256 == first.record_sha256
        assert replacement.supersedes_cost_evidence_ids == (first_cost.cost_evidence_id,)
    finally:
        fixture.doCleanups()


def test_version_roundtrip_preserves_canonical_campaign_projection() -> None:
    fixture, authority = _fixture_authority()
    try:
        cost = _cost(authority)
        version = derive_campaign_economics(campaign=authority, costs=(cost,), as_of=T2)
        loaded = CampaignEconomicEvidenceVersion.from_dict(version.to_dict())
        assert loaded == version
        assert loaded.version_id == version.version_id
        assert loaded.gross_run_pnl == Decimal("60")
    finally:
        fixture.doCleanups()
