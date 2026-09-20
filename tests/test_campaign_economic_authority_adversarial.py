from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.campaign_cost_evidence import derive_campaign_economics
from autosport.campaign_economic_authority import (
    CanonicalMembershipRef,
    CanonicalSessionRef,
)
from autosport.campaign_economic_store import (
    CampaignEconomicEvidenceStore,
    CampaignEconomicStoreError,
)
from test_campaign_cost_evidence import T2, _cost, _fixture_authority


def _store(tmp_path: Path, authority) -> CampaignEconomicEvidenceStore:
    return CampaignEconomicEvidenceStore(
        tmp_path / "workspace",
        campaign=authority,
        authority_root=tmp_path / "external-authority",
    )


def test_store_rejects_caller_mutated_gross_pnl_before_persistence(tmp_path: Path) -> None:
    fixture, authority = _fixture_authority()
    try:
        valid = derive_campaign_economics(
            campaign=authority,
            costs=(_cost(authority),),
            as_of=T2,
        )
        forged_projection = replace(
            valid.campaign_authority,
            gross_run_pnl=Decimal("999"),
        )
        forged = replace(valid, campaign_authority=forged_projection)

        with pytest.raises(CampaignEconomicStoreError, match="forged"):
            _store(tmp_path, authority).append(forged)
    finally:
        fixture.doCleanups()


def test_store_rejects_caller_mutated_session_digest_before_persistence(tmp_path: Path) -> None:
    fixture, authority = _fixture_authority()
    try:
        valid = derive_campaign_economics(
            campaign=authority,
            costs=(_cost(authority),),
            as_of=T2,
        )
        original = valid.campaign_authority.session_refs[0]
        forged_session = CanonicalSessionRef(
            evidence_id=original.evidence_id,
            evidence_sha256="1" * 64,
        )
        forged_projection = replace(
            valid.campaign_authority,
            session_refs=(forged_session,),
        )
        forged = replace(valid, campaign_authority=forged_projection)

        with pytest.raises(CampaignEconomicStoreError, match="forged"):
            _store(tmp_path, authority).append(forged)
    finally:
        fixture.doCleanups()


@pytest.mark.parametrize("kind,digit", [("RUN", "2"), ("EVALUATION", "3")])
def test_store_rejects_caller_mutated_run_or_evaluation_membership(
    tmp_path: Path,
    kind: str,
    digit: str,
) -> None:
    fixture, authority = _fixture_authority()
    try:
        valid = derive_campaign_economics(
            campaign=authority,
            costs=(_cost(authority),),
            as_of=T2,
        )
        forged_memberships = []
        for item in valid.campaign_authority.membership_refs:
            if item.kind == kind:
                forged_memberships.append(
                    CanonicalMembershipRef(
                        kind=item.kind,
                        evidence_id=item.evidence_id,
                        sha256=digit * 64,
                    )
                )
            else:
                forged_memberships.append(item)
        forged_projection = replace(
            valid.campaign_authority,
            membership_refs=tuple(sorted(forged_memberships)),
        )
        forged = replace(valid, campaign_authority=forged_projection)

        with pytest.raises(CampaignEconomicStoreError, match="forged"):
            _store(tmp_path, authority).append(forged)
    finally:
        fixture.doCleanups()


def test_exact_live_finalized_campaign_projection_can_persist(tmp_path: Path) -> None:
    fixture, authority = _fixture_authority()
    try:
        valid = derive_campaign_economics(
            campaign=authority,
            costs=(_cost(authority),),
            as_of=T2,
        )
        store = _store(tmp_path, authority)
        assert store.append(valid) == valid.version_id
        assert store.latest() == valid
    finally:
        fixture.doCleanups()
