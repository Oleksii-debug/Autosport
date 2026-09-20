from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.betfair_campaign_economic_store as durable_store
import autosport.betfair_commission_cost_evidence as commission_bridge
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


def test_provider_correction_advances_durable_economics_and_survives_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_receipt = _receipt()
    source, campaign, origin_client = _authorities(
        monkeypatch,
        receipt=first_receipt,
    )
    provider_scope = _scope()
    workspace = tmp_path / "correction-workspace"
    authority_root = tmp_path / "correction-authority"
    store = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=provider_scope,
        authority_root=authority_root,
    )

    first_id = store.append_betfair_commission(
        receipt_id=first_receipt.receipt_id,
        record_sha256=first_receipt.record_sha256,
        as_of=NOW,
    )
    first = store.latest()
    assert first is not None
    assert first.version_id == first_id
    first_cost = first.costs[0]

    corrected_receipt = _receipt(
        commission=Decimal("3.50"),
        supersedes_receipt_id=first_receipt.receipt_id,
    )
    monkeypatch.setattr(
        commission_bridge._source_origin,
        "resolve_bound_receipt",
        lambda source, *, receipt_id, record_sha256, as_of: (
            corrected_receipt,
            origin_client,
        ),
    )

    corrected_id = store.append_betfair_commission(
        receipt_id=corrected_receipt.receipt_id,
        record_sha256=corrected_receipt.record_sha256,
        as_of=NOW,
    )
    corrected = store.latest()
    assert corrected is not None
    assert corrected.version_id == corrected_id
    assert corrected.previous_version_id == first.version_id
    assert len(corrected.costs) == 1
    corrected_cost = corrected.costs[0]
    assert corrected_cost.source.evidence_id == corrected_receipt.receipt_id
    assert corrected_cost.amount == Decimal("3.50")
    assert corrected_cost.supersedes_cost_evidence_ids == (
        first_cost.cost_evidence_id,
    )
    assert corrected_cost.cost_evidence_id != first_cost.cost_evidence_id
    assert store.verify_chain() == (first, corrected)

    restarted = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=provider_scope,
        authority_root=authority_root,
    )
    assert restarted.latest() == corrected
    assert restarted.verify_chain() == (first, corrected)
    assert (
        restarted.append_betfair_commission(
            receipt_id=corrected_receipt.receipt_id,
            record_sha256=corrected_receipt.record_sha256,
            as_of=NOW,
        )
        == corrected.version_id
    )
    assert restarted.verify_chain() == (first, corrected)


def test_provider_correction_rejects_wrong_predecessor_receipt_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_receipt = _receipt()
    source, campaign, origin_client = _authorities(
        monkeypatch,
        receipt=first_receipt,
    )
    provider_scope = _scope()
    store = BetfairCampaignEconomicEvidenceStore(
        tmp_path / "wrong-link-workspace",
        campaign=campaign,
        source=source,
        provider_scope=provider_scope,
        authority_root=tmp_path / "wrong-link-authority",
    )
    store.append_betfair_commission(
        receipt_id=first_receipt.receipt_id,
        record_sha256=first_receipt.record_sha256,
        as_of=NOW,
    )
    first = store.latest()
    assert first is not None

    wrong = _receipt(
        commission=Decimal("4.00"),
        supersedes_receipt_id="f" * 64,
    )
    monkeypatch.setattr(
        commission_bridge._source_origin,
        "resolve_bound_receipt",
        lambda source, *, receipt_id, record_sha256, as_of: (wrong, origin_client),
    )
    with pytest.raises(
        commission_bridge.BetfairCommissionCostEvidenceError,
        match="does not exactly supersede active Betfair campaign cost evidence",
    ):
        store.append_betfair_commission(
            receipt_id=wrong.receipt_id,
            record_sha256=wrong.record_sha256,
            as_of=NOW,
        )
    assert store.latest() == first
    assert store.verify_chain() == (first,)
