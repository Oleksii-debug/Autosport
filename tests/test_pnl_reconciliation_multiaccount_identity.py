from __future__ import annotations

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


_CREATED = "2026-09-29T07:00:00+00:00"
_RESERVED = "2026-09-29T07:00:05+00:00"
_SUBMITTED = "2026-09-29T07:00:10+00:00"
_ACKED = "2026-09-29T07:00:15+00:00"
_EXPIRES = "2026-09-29T07:01:00+00:00"


def _accepted_execution(
    path: Path,
    *,
    account_id: str,
    suffix: str,
) -> RealExecutionLedger:
    ledger = RealExecutionLedger(path)
    action = ExecutionAction(
        action_id=f"action-{suffix}",
        bookmaker_id="betfair",
        account_id=account_id,
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        requested_odds="2.00",
        requested_stake="4.00",
        quote_id=f"quote-{suffix}",
        quote_observed_at=_CREATED,
        expires_at=_EXPIRES,
    )
    plan = ExecutionPlan(
        plan_id=f"plan-{suffix}",
        bookmaker_profile_version="profile-v1",
        decision_id=f"decision-{suffix}",
        approval_id=f"approval-{suffix}",
        created_at=_CREATED,
        actions=(action,),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id=f"attempt-{suffix}",
        reserved_at=_RESERVED,
    )
    ledger.mark_submitted(f"attempt-{suffix}", submitted_at=_SUBMITTED)
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id=f"attempt-{suffix}",
            external_receipt_id="shared-receipt",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at=_ACKED,
            accepted_odds="2.00",
            accepted_stake="4.00",
        )
    )
    return ledger


def test_canonical_receipt_identity_is_account_scoped_and_restart_safe(
    tmp_path: Path,
) -> None:
    first_execution = _accepted_execution(
        tmp_path / "execution-a.jsonl",
        account_id="acct-a",
        suffix="a",
    )
    second_execution = _accepted_execution(
        tmp_path / "execution-b.jsonl",
        account_id="acct-b",
        suffix="b",
    )
    journal_path = tmp_path / "pnl.jsonl"
    journal = PnLReconciliationJournal(journal_path, currency="USD")

    journal.record_accepted_execution(
        event_id="accepted-a",
        execution_ledger=first_execution,
        bookmaker_id="betfair",
        account_id="acct-a",
        external_receipt_id="shared-receipt",
    )
    accepted = journal.record_accepted_execution(
        event_id="accepted-b",
        execution_ledger=second_execution,
        bookmaker_id="betfair",
        account_id="acct-b",
        external_receipt_id="shared-receipt",
    )
    assert accepted.accepted_order_count == 2
    assert accepted.open_back_stake == Decimal("8")

    before_ambiguous = journal_path.read_bytes()
    with pytest.raises(ValueError, match="ambiguous across provider accounts"):
        journal.record_settlement_revision(
            event_id="ambiguous",
            provider_source_id="betfair",
            provider_order_id="shared-receipt",
            revision_seq=1,
            cumulative_settled_stake="4",
            cumulative_realized_pnl="0",
        )
    assert journal.faulted is False
    assert journal_path.read_bytes() == before_ambiguous

    first_settled = journal.record_settlement_revision(
        event_id="settled-a",
        provider_source_id="betfair",
        provider_account_id="acct-a",
        provider_order_id="shared-receipt",
        revision_seq=1,
        cumulative_settled_stake="4",
        cumulative_realized_pnl="4",
    )
    assert first_settled.realized_pnl == Decimal("4")
    assert first_settled.open_back_stake == Decimal("4")

    second_settled = journal.record_settlement_revision(
        event_id="settled-b",
        provider_source_id="betfair",
        provider_account_id="acct-b",
        provider_order_id="shared-receipt",
        revision_seq=1,
        cumulative_settled_stake="4",
        cumulative_realized_pnl="-4",
    )
    assert second_settled.accepted_order_count == 2
    assert second_settled.settlement_revision_count == 2
    assert second_settled.realized_pnl == Decimal("0")
    assert second_settled.open_back_stake == Decimal("0")
    assert second_settled.positive_authority_verified is False

    reopened = PnLReconciliationJournal(journal_path, currency="USD").snapshot()
    assert reopened == second_settled
