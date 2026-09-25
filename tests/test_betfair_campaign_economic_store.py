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

def test_multiple_market_commissions_share_one_durable_campaign_economics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_receipt = _receipt(
        market_id="1.234",
        commission=Decimal("2.25"),
    )
    source, campaign, origin_client = _authorities(
        monkeypatch,
        receipt=first_receipt,
    )
    second_receipt = _receipt(
        market_id="1.999",
        commission=Decimal("1.25"),
    )
    receipts = {
        first_receipt.receipt_id: first_receipt,
        second_receipt.receipt_id: second_receipt,
    }

    def resolve_receipt(source, *, receipt_id, record_sha256, as_of):
        receipt = receipts[receipt_id]
        assert record_sha256 == receipt.record_sha256
        return receipt, origin_client

    monkeypatch.setattr(
        commission_bridge._source_origin,
        "resolve_bound_receipt",
        resolve_receipt,
    )

    workspace = tmp_path / "multi-market-workspace"
    authority_root = tmp_path / "multi-market-authority"
    first_store = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=_scope(market_id="1.234"),
        authority_root=authority_root,
    )
    first_id = first_store.append_betfair_commission(
        receipt_id=first_receipt.receipt_id,
        record_sha256=first_receipt.record_sha256,
        as_of=NOW,
    )
    first_version = first_store.latest()
    assert first_version is not None
    assert first_version.version_id == first_id

    second_store = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=_scope(market_id="1.999"),
        authority_root=authority_root,
    )
    second_id = second_store.append_betfair_commission(
        receipt_id=second_receipt.receipt_id,
        record_sha256=second_receipt.record_sha256,
        as_of=NOW,
    )
    combined = second_store.latest()
    assert combined is not None
    assert combined.version_id == second_id
    assert combined.previous_version_id == first_version.version_id
    assert {
        (item.source.evidence_id, item.amount)
        for item in combined.costs
    } == {
        (first_receipt.receipt_id, Decimal("2.25")),
        (second_receipt.receipt_id, Decimal("1.25")),
    }
    assert durable_store._UNRESOLVED_REASON not in combined.incomplete_reasons
    assert second_store.verify_chain() == (first_version, combined)

    restarted_second = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=_scope(market_id="1.999"),
        authority_root=authority_root,
    )
    assert restarted_second.latest() == combined
    assert (
        restarted_second.append_betfair_commission(
            receipt_id=second_receipt.receipt_id,
            record_sha256=second_receipt.record_sha256,
            as_of=NOW,
        )
        == combined.version_id
    )
    assert restarted_second.verify_chain() == (first_version, combined)

    restarted_first = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=_scope(market_id="1.234"),
        authority_root=authority_root,
    )
    assert (
        restarted_first.append_betfair_commission(
            receipt_id=first_receipt.receipt_id,
            record_sha256=first_receipt.record_sha256,
            as_of=NOW,
        )
        == combined.version_id
    )
    assert restarted_first.verify_chain() == (first_version, combined)


def test_multi_market_correction_replaces_only_exact_market_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_receipt = _receipt(
        market_id="1.234",
        commission=Decimal("2.25"),
    )
    source, campaign, origin_client = _authorities(
        monkeypatch,
        receipt=first_receipt,
    )
    second_receipt = _receipt(
        market_id="1.999",
        commission=Decimal("1.25"),
    )
    receipts = {
        first_receipt.receipt_id: first_receipt,
        second_receipt.receipt_id: second_receipt,
    }

    def resolve_receipt(source, *, receipt_id, record_sha256, as_of):
        receipt = receipts[receipt_id]
        assert record_sha256 == receipt.record_sha256
        return receipt, origin_client

    monkeypatch.setattr(
        commission_bridge._source_origin,
        "resolve_bound_receipt",
        resolve_receipt,
    )

    workspace = tmp_path / "selective-correction-workspace"
    authority_root = tmp_path / "selective-correction-authority"
    first_store = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=_scope(market_id="1.234"),
        authority_root=authority_root,
    )
    first_store.append_betfair_commission(
        receipt_id=first_receipt.receipt_id,
        record_sha256=first_receipt.record_sha256,
        as_of=NOW,
    )
    first_version = first_store.latest()
    assert first_version is not None
    first_cost = first_version.costs[0]

    second_store = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=_scope(market_id="1.999"),
        authority_root=authority_root,
    )
    second_store.append_betfair_commission(
        receipt_id=second_receipt.receipt_id,
        record_sha256=second_receipt.record_sha256,
        as_of=NOW,
    )
    before_correction = second_store.latest()
    assert before_correction is not None
    second_cost = next(
        item
        for item in before_correction.costs
        if item.source.evidence_id == second_receipt.receipt_id
    )

    corrected_receipt = _receipt(
        market_id="1.999",
        commission=Decimal("1.75"),
        supersedes_receipt_id=second_receipt.receipt_id,
    )
    receipts[corrected_receipt.receipt_id] = corrected_receipt

    corrected_id = second_store.append_betfair_commission(
        receipt_id=corrected_receipt.receipt_id,
        record_sha256=corrected_receipt.record_sha256,
        as_of=NOW,
    )
    corrected = second_store.latest()
    assert corrected is not None
    assert corrected.version_id == corrected_id
    assert corrected.previous_version_id == before_correction.version_id
    assert len(corrected.costs) == 2

    retained_first = next(
        item
        for item in corrected.costs
        if item.source.evidence_id == first_receipt.receipt_id
    )
    corrected_second = next(
        item
        for item in corrected.costs
        if item.source.evidence_id == corrected_receipt.receipt_id
    )
    assert retained_first == first_cost
    assert second_cost.cost_evidence_id not in {
        item.cost_evidence_id for item in corrected.costs
    }
    assert corrected_second.amount == Decimal("1.75")
    assert corrected_second.supersedes_cost_evidence_ids == (
        second_cost.cost_evidence_id,
    )
    assert durable_store._UNRESOLVED_REASON not in corrected.incomplete_reasons
    assert second_store.verify_chain() == (
        first_version,
        before_correction,
        corrected,
    )

    restarted_second = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=_scope(market_id="1.999"),
        authority_root=authority_root,
    )
    assert restarted_second.latest() == corrected
    assert (
        restarted_second.append_betfair_commission(
            receipt_id=corrected_receipt.receipt_id,
            record_sha256=corrected_receipt.record_sha256,
            as_of=NOW,
        )
        == corrected.version_id
    )
    assert restarted_second.verify_chain() == (
        first_version,
        before_correction,
        corrected,
    )

