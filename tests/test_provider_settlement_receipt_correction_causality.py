from datetime import datetime, timezone

import pytest

from autosport.provider_settlement_receipt import (
    ProviderSettlementReceipt,
    ProviderSettlementReceiptError,
    SettlementDisposition,
    verify_settlement_revision,
)


UTC = timezone.utc


def _initial() -> ProviderSettlementReceipt:
    return ProviderSettlementReceipt(
        provider="betfair",
        provider_receipt_id="settlement-1",
        execution_id="execution-1",
        market_id="1.2345",
        selection_id="123",
        disposition=SettlementDisposition.WIN,
        settled_at=datetime(2026, 9, 21, 18, 0, tzinfo=UTC),
        rule_id="football.match-odds",
        rule_version="2026-09-01",
        rule_sha256="a" * 64,
        provider_evidence_sha256="b" * 64,
    )


def test_semantic_correction_requires_changed_authoritative_basis() -> None:
    previous = _initial()
    current = ProviderSettlementReceipt(
        provider=previous.provider,
        provider_receipt_id=previous.provider_receipt_id,
        execution_id=previous.execution_id,
        market_id=previous.market_id,
        selection_id=previous.selection_id,
        disposition=SettlementDisposition.LOSS,
        settled_at=previous.settled_at,
        rule_id=previous.rule_id,
        rule_version=previous.rule_version,
        rule_sha256=previous.rule_sha256,
        provider_evidence_sha256=previous.provider_evidence_sha256,
        revision=1,
        supersedes_receipt_sha256=previous.receipt_sha256,
    )

    with pytest.raises(ProviderSettlementReceiptError):
        verify_settlement_revision(previous, current)
