from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.betfair_campaign_economic_store as durable_store
from autosport.betfair_campaign_economic_store import (
    BetfairCampaignEconomicEvidenceStore,
)
from autosport.campaign_cost_evidence import EconomicCompleteness
from autosport.campaign_economic_store import CampaignEconomicStoreError
from test_betfair_commission_cost_evidence import NOW, _authorities, _receipt, _scope


def test_source_qualified_append_is_live_gated_durable_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    provider_scope = _scope()
    workspace = tmp_path / "economic-workspace"
    authority_root = tmp_path / "external-monotonic-authority"
    store = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=provider_scope,
        authority_root=authority_root,
    )

    qualified = store.derive_betfair_commission(
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )
    with pytest.raises(
        CampaignEconomicStoreError,
        match="lacks live verification or durable publication authority",
    ):
        store.append(qualified)
    assert store.latest() is None

    forged = replace(
        qualified,
        known_cost_total=Decimal("999"),
        net_after_known_costs=Decimal("-939"),
        completeness=EconomicCompleteness.COMPLETE_NET_ECONOMICS,
        incomplete_reasons=(),
    )
    with pytest.raises(CampaignEconomicStoreError):
        store.append(forged)
    assert store.latest() is None

    version_id = store.append_betfair_commission(
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )
    assert version_id == qualified.version_id
    assert store.latest() == qualified

    restarted = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=provider_scope,
        authority_root=authority_root,
    )
    assert restarted.latest() == qualified
    assert restarted.verify_chain() == (qualified,)
    assert (
        restarted.append_betfair_commission(
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )
        == qualified.version_id
    )
    assert restarted.verify_chain() == (qualified,)

    def unavailable_source(**_kwargs):
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(
        durable_store,
        "issue_betfair_commission_cost_evidence",
        unavailable_source,
    )
    with pytest.raises(
        CampaignEconomicStoreError,
        match="failed live source re-verification",
    ):
        restarted.append_betfair_commission(
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )
    assert restarted.verify_chain() == (qualified,)


def test_source_qualified_terminal_prepare_recovers_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    source, campaign, _ = _authorities(monkeypatch, receipt=receipt)
    provider_scope = _scope()
    workspace = tmp_path / "crash-workspace"
    authority_root = tmp_path / "crash-authority"
    store = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=provider_scope,
        authority_root=authority_root,
    )
    qualified = store.derive_betfair_commission(
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=NOW,
    )

    def crash_before_commit(**_kwargs):
        raise RuntimeError("simulated process loss before authority commit")

    monkeypatch.setattr(store._authority, "commit", crash_before_commit)
    with pytest.raises(RuntimeError, match="simulated process loss"):
        store.append_betfair_commission(
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )

    restarted = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=provider_scope,
        authority_root=authority_root,
    )
    assert restarted.latest() == qualified
    assert restarted.verify_chain() == (qualified,)
    assert (
        restarted.append_betfair_commission(
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=NOW,
        )
        == qualified.version_id
    )
    assert restarted.verify_chain() == (qualified,)
