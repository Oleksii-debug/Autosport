from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.campaign_cost_evidence import (
    CostClass,
    CostEvidenceError,
    derive_campaign_economics,
)
from autosport.campaign_economic_store import CampaignEconomicEvidenceStore
from test_campaign_cost_evidence import T1, T2, _cost, _fixture_authority


def test_observed_incurred_known_cost_requires_incurred_timestamp() -> None:
    fixture, authority = _fixture_authority()
    try:
        valid = _cost(authority)
        with pytest.raises(CostEvidenceError, match="requires incurred_at"):
            replace(valid, incurred_at=None)
    finally:
        fixture.doCleanups()


def test_genesis_version_cannot_claim_supersession_without_predecessor() -> None:
    fixture, authority = _fixture_authority()
    try:
        forged = _cost(authority, supersedes=("f" * 64,))
        with pytest.raises(CostEvidenceError, match="first economic version"):
            derive_campaign_economics(
                campaign=authority,
                costs=(forged,),
                as_of=T1,
            )
    finally:
        fixture.doCleanups()


def test_retained_correction_keeps_prior_provenance_across_restart(
    tmp_path: Path,
) -> None:
    fixture, authority = _fixture_authority()
    try:
        original = _cost(authority, amount=Decimal("2"))
        first = derive_campaign_economics(
            campaign=authority,
            costs=(original,),
            as_of=T1,
        )
        corrected = _cost(
            authority,
            source_digit="b",
            amount=Decimal("4"),
            available_at=T2,
            supersedes=(original.cost_evidence_id,),
        )
        second = derive_campaign_economics(
            campaign=authority,
            costs=(corrected,),
            as_of=T2,
            previous=first,
        )
        additional = _cost(
            authority,
            cost_class=CostClass.FIXED_CAMPAIGN,
            source_digit="c",
            amount=Decimal("1"),
            available_at=T2,
        )
        third = derive_campaign_economics(
            campaign=authority,
            costs=(corrected, additional),
            as_of=T2,
            previous=second,
        )
        assert corrected in third.costs
        assert corrected.supersedes_cost_evidence_ids == (original.cost_evidence_id,)
        assert additional in third.costs

        workspace = tmp_path / "economic-workspace"
        authority_root = tmp_path / "external-monotonic-authority"
        store = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        assert store.append(first) == first.version_id
        assert store.append(second) == second.version_id
        assert store.append(third) == third.version_id

        restarted = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        assert restarted.latest() == third
        assert restarted.verify_chain() == (first, second, third)
    finally:
        fixture.doCleanups()


def test_new_correction_cannot_supersede_already_replaced_dead_evidence() -> None:
    fixture, authority = _fixture_authority()
    try:
        original = _cost(authority, amount=Decimal("2"))
        first = derive_campaign_economics(
            campaign=authority,
            costs=(original,),
            as_of=T1,
        )
        corrected = _cost(
            authority,
            source_digit="b",
            amount=Decimal("4"),
            available_at=T2,
            supersedes=(original.cost_evidence_id,),
        )
        second = derive_campaign_economics(
            campaign=authority,
            costs=(corrected,),
            as_of=T2,
            previous=first,
        )
        stale_correction = _cost(
            authority,
            source_digit="c",
            amount=Decimal("5"),
            available_at=T2,
            supersedes=(original.cost_evidence_id,),
        )
        with pytest.raises(CostEvidenceError, match="only predecessor"):
            derive_campaign_economics(
                campaign=authority,
                costs=(corrected, stale_correction),
                as_of=T2,
                previous=second,
            )
    finally:
        fixture.doCleanups()
