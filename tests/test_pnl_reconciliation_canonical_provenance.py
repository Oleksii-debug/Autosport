from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.pnl_reconciliation import PnLReconciliationJournal
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)


_CREATED = "2026-09-23T10:00:00+00:00"
_RESERVED = "2026-09-23T10:00:05+00:00"
_SUBMITTED = "2026-09-23T10:00:10+00:00"
_ACKED = "2026-09-23T10:00:15+00:00"
_EXPIRES = "2026-09-23T10:01:00+00:00"


def _accepted_execution_ledger(
    path: Path,
    *,
    side: str = "BACK",
    accepted_stake: str = "4.25",
    accepted_odds: str = "2.50",
) -> RealExecutionLedger:
    ledger = RealExecutionLedger(path)
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side=side,
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id="quote-1",
        quote_observed_at=_CREATED,
        expires_at=_EXPIRES,
    )
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=_CREATED,
        actions=(action,),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id="plan-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserved_at=_RESERVED,
    )
    ledger.mark_submitted("attempt-1", submitted_at=_SUBMITTED)
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-1",
            external_receipt_id="receipt-1",
            status=AcknowledgementStatus.PARTIAL,
            acknowledged_at=_ACKED,
            accepted_odds=accepted_odds,
            accepted_stake=accepted_stake,
        )
    )
    return ledger


def _journal(path: Path) -> PnLReconciliationJournal:
    return PnLReconciliationJournal(
        path,
        currency="EUR",
        liability_quantum="0.01",
    )


def test_canonical_accepted_execution_re_resolves_ledger_economics_without_promoting_authority(
    tmp_path: Path,
) -> None:
    execution = _accepted_execution_ledger(tmp_path / "execution.jsonl")
    journal_path = tmp_path / "pnl.jsonl"
    journal = _journal(journal_path)

    snapshot = journal.record_accepted_execution(
        event_id="accepted-1",
        execution_ledger=execution,
        bookmaker_id="betfair",
        account_id="acct-1",
        external_receipt_id="receipt-1",
    )

    assert snapshot.accepted_order_count == 1
    assert snapshot.open_back_stake == Decimal("4.25")
    assert snapshot.open_lay_liability == Decimal("0")
    assert snapshot.execution_provenance_bound is False
    assert snapshot.execution_evidence_verified is False
    assert snapshot.positive_authority_verified is False
    assert journal.snapshot().execution_provenance_bound is False

    event = json.loads(journal_path.read_text(encoding="utf-8"))
    assert event["event_type"] == "accepted_execution"
    assert event["provider_source_id"] == "betfair"
    assert event["provider_order_id"] == "receipt-1"
    assert event["accepted_stake"] == "4.25"
    assert event["accepted_odds"] == "2.5"
    assert event["execution_account_id"] == "acct-1"
    assert event["execution_action_id"] == "action-1"
    assert event["execution_attempt_id"] == "attempt-1"
    assert event["execution_ledger_sha256"] == execution.verified_snapshot().sha256


def test_wrong_receipt_cannot_mint_accepted_economics(tmp_path: Path) -> None:
    execution = _accepted_execution_ledger(tmp_path / "execution.jsonl")
    journal_path = tmp_path / "pnl.jsonl"
    journal = _journal(journal_path)

    with pytest.raises(KeyError):
        journal.record_accepted_execution(
            event_id="accepted-forged",
            execution_ledger=execution,
            bookmaker_id="betfair",
            account_id="acct-1",
            external_receipt_id="caller-invented-receipt",
        )

    assert not journal_path.exists()


def test_scalar_manual_economics_remain_mechanically_non_authoritative(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "pnl.jsonl")

    scalar = journal.record_accepted_order(
        event_id="manual-accepted",
        provider_source_id="caller",
        provider_order_id="caller-order",
        side="BACK",
        accepted_stake="999999.00",
        accepted_odds="99.0",
    )
    assert scalar.execution_provenance_bound is False
    assert scalar.execution_evidence_verified is False
    assert scalar.positive_authority_verified is False

    with pytest.raises(ValueError):
        replace(scalar, positive_authority_verified=True)


def test_forged_settlement_values_cannot_promote_any_authority(tmp_path: Path) -> None:
    execution = _accepted_execution_ledger(tmp_path / "execution.jsonl")
    journal = _journal(tmp_path / "pnl.jsonl")
    journal.record_accepted_execution(
        event_id="accepted-1",
        execution_ledger=execution,
        bookmaker_id="betfair",
        account_id="acct-1",
        external_receipt_id="receipt-1",
    )

    # This is the exact gross BACK win bound for 4.25 @ 2.50. It is mechanically
    # possible but remains caller-derived settlement data and therefore cannot mint
    # provider/economic authority.
    snapshot = journal.record_settlement_revision(
        event_id="settlement-caller-1",
        provider_source_id="betfair",
        provider_order_id="receipt-1",
        revision_seq=1,
        cumulative_settled_stake="4.25",
        cumulative_realized_pnl="6.375",
    )

    assert snapshot.execution_provenance_bound is False
    assert snapshot.execution_evidence_verified is False
    assert snapshot.realized_pnl == Decimal("6.375")
    assert snapshot.positive_authority_verified is False


def test_replayed_jsonl_does_not_mint_execution_provenance_authority(tmp_path: Path) -> None:
    execution = _accepted_execution_ledger(tmp_path / "execution.jsonl")
    journal_path = tmp_path / "pnl.jsonl"
    journal = _journal(journal_path)
    issued = journal.record_accepted_execution(
        event_id="accepted-1",
        execution_ledger=execution,
        bookmaker_id="betfair",
        account_id="acct-1",
        external_receipt_id="receipt-1",
    )
    assert issued.execution_provenance_bound is False

    reopened = _journal(journal_path).snapshot()

    assert reopened.accepted_order_count == 1
    assert reopened.open_back_stake == Decimal("4.25")
    assert reopened.execution_provenance_bound is False
    assert reopened.execution_evidence_verified is False
    assert reopened.positive_authority_verified is False


def test_copied_canonical_accepted_execution_bytes_do_not_mint_provenance(tmp_path: Path) -> None:
    execution = _accepted_execution_ledger(tmp_path / "execution.jsonl")
    source_path = tmp_path / "source-pnl.jsonl"
    source = _journal(source_path)
    original = source.record_accepted_execution(
        event_id="accepted-1",
        execution_ledger=execution,
        bookmaker_id="betfair",
        account_id="acct-1",
        external_receipt_id="receipt-1",
    )
    assert original.execution_provenance_bound is False

    forged_path = tmp_path / "copied-pnl.jsonl"
    forged_path.write_bytes(source_path.read_bytes())
    copied = _journal(forged_path).snapshot()

    assert copied.accepted_order_count == 1
    assert copied.open_back_stake == Decimal("4.25")
    assert copied.execution_provenance_bound is False
    assert copied.execution_evidence_verified is False
    assert copied.positive_authority_verified is False
