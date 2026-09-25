from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import autosport.betfair_settlement_revisions as settlement


def _forged_but_structurally_valid_revision() -> settlement.BetfairSettlementRevision:
    price_requested = Decimal("2.00")
    price_matched = Decimal("2.00")
    size_settled = Decimal("10.00")
    provider_profit = Decimal("10.00")
    semantic_payload = {
        "bookmaker_id": "betfair",
        "account_id": "acct-forged",
        "adapter_id": settlement.BETFAIR_ADAPTER_ID,
        "adapter_version": settlement.BETFAIR_ADAPTER_VERSION,
        "plan_id": "plan-forged",
        "action_id": "action-forged",
        "attempt_id": "attempt-forged",
        "external_bet_id": "bet-forged",
        "event_id": "event-forged",
        "market_id": "market-forged",
        "selection_id": "selection-forged",
        "side": "BACK",
        "provider_status": "SETTLED",
        "placed_date": "2026-09-25T10:00:00+00:00",
        "settled_date": "2026-09-25T11:00:00+00:00",
        "price_requested": format(price_requested, "f"),
        "price_matched": format(price_matched, "f"),
        "size_settled": format(size_settled, "f"),
        "provider_profit": format(provider_profit, "f"),
    }
    content_sha256 = settlement._digest(semantic_payload)
    revision_id = settlement._digest(
        {
            "schema": settlement._SCHEMA,
            "schema_version": settlement._SCHEMA_VERSION,
            "previous_revision_id": None,
            "revision_number": 1,
            "content_sha256": content_sha256,
        }
    )
    return settlement.BetfairSettlementRevision(
        revision_id=revision_id,
        previous_revision_id=None,
        revision_number=1,
        bookmaker_id="betfair",
        account_id="acct-forged",
        adapter_id=settlement.BETFAIR_ADAPTER_ID,
        adapter_version=settlement.BETFAIR_ADAPTER_VERSION,
        plan_id="plan-forged",
        action_id="action-forged",
        attempt_id="attempt-forged",
        external_bet_id="bet-forged",
        event_id="event-forged",
        market_id="market-forged",
        selection_id="selection-forged",
        side="BACK",
        provider_status="SETTLED",
        placed_date="2026-09-25T10:00:00+00:00",
        settled_date="2026-09-25T11:00:00+00:00",
        price_requested=price_requested,
        price_matched=price_matched,
        size_settled=size_settled,
        provider_profit=provider_profit,
        available_at="2026-09-25T11:00:01+00:00",
        source_payload_sha256="1" * 64,
        capture_evidence_sha256="2" * 64,
        content_sha256=content_sha256,
    )


def _write_unanchored_valid_journal(path: Path) -> None:
    revision = _forged_but_structurally_valid_revision()
    unsigned = {
        "schema": settlement._SCHEMA,
        "schema_version": settlement._SCHEMA_VERSION,
        "previous_record_sha256": None,
        "revision": revision.to_dict(),
    }
    record = {**unsigned, "record_sha256": settlement._digest(unsigned)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(settlement._canonical(record) + "\n", encoding="utf-8")


def test_hash_valid_preexisting_journal_without_monotonic_history_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_root = tmp_path / "monotonic-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
    )
    journal = tmp_path / "settlement.jsonl"
    _write_unanchored_valid_journal(journal)

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="non-empty settlement journal lacks independent monotonic authority",
    ):
        settlement.BetfairSettlementRevisionStore(journal)


def test_empty_new_settlement_store_still_bootstraps_without_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_root = tmp_path / "monotonic-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
    )
    store = settlement.BetfairSettlementRevisionStore(
        tmp_path / "new-settlement.jsonl"
    )

    assert store.revisions == ()
