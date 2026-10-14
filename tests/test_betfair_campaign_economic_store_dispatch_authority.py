from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.betfair_campaign_economic_store as durable_store
import autosport.betfair_commission_cost_evidence as commission_bridge
from autosport.betfair_campaign_economic_store import BetfairCampaignEconomicEvidenceStore
from autosport.campaign_economic_store import CampaignEconomicStoreError
from test_betfair_commission_cost_evidence import NOW, _authorities, _receipt, _scope


def test_consumer_issuer_rebind_cannot_mint_source_qualified_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    provider_scope = _scope()
    canonical = commission_bridge.issue_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=provider_scope,
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )
    forged = replace(canonical, amount=Decimal("999"))
    assert forged.cost_evidence_id != canonical.cost_evidence_id

    store = BetfairCampaignEconomicEvidenceStore(
        tmp_path / "economic-workspace",
        campaign=campaign,
        source=source,
        provider_scope=provider_scope,
        authority_root=tmp_path / "external-monotonic-authority",
    )

    # Before the durable derivation captured the canonical issuer at class-definition
    # time, both derivation and live verification resolved this mutable module alias.
    # Returning one internally self-consistent forged CostEvidence from both calls
    # could therefore mint a source-qualified durable version.
    monkeypatch.setattr(
        durable_store,
        "issue_betfair_commission_cost_evidence",
        lambda **_kwargs: forged,
    )

    with pytest.raises(
        CampaignEconomicStoreError,
        match="changed during durable economic admission",
    ):
        store.append_betfair_commission(
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )

    assert store.latest() is None
