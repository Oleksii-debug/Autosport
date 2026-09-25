"""Dependent falsifier for PR #990 multi-market commission retry idempotency."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.betfair_commission_cost_evidence as commission_bridge
from autosport.betfair_campaign_economic_store import (
    BetfairCampaignEconomicEvidenceStore,
)
from test_betfair_commission_cost_evidence import NOW, _authorities, _receipt, _scope


def test_active_receipt_retry_after_later_market_append_accepts_later_as_of(
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

    workspace = tmp_path / "retry-time-workspace"
    authority_root = tmp_path / "retry-time-authority"
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

    second_store = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=_scope(market_id="1.999"),
        authority_root=authority_root,
    )
    second_as_of = NOW + timedelta(minutes=1)
    combined_id = second_store.append_betfair_commission(
        receipt_id=second_receipt.receipt_id,
        record_sha256=second_receipt.record_sha256,
        as_of=second_as_of,
    )
    combined = second_store.latest()
    assert combined is not None
    assert combined.version_id == combined_id
    assert combined.as_of == second_as_of

    restarted_first = BetfairCampaignEconomicEvidenceStore(
        workspace,
        campaign=campaign,
        source=source,
        provider_scope=_scope(market_id="1.234"),
        authority_root=authority_root,
    )
    retry_as_of = second_as_of + timedelta(minutes=1)
    before_retry = restarted_first.verify_chain()

    # A later causal cutoff can safely re-resolve the same still-active provider
    # receipt. Retry identity must not depend on reproducing the latest economic
    # version's exact as_of timestamp.
    assert (
        restarted_first.append_betfair_commission(
            receipt_id=first_receipt.receipt_id,
            record_sha256=first_receipt.record_sha256,
            as_of=retry_as_of,
        )
        == combined_id
    )
    assert restarted_first.latest() == combined
    assert restarted_first.verify_chain() == before_retry
