from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport.pnl_reconciliation import PnLReconciliationJournal


def _proven_journal(tmp_path: Path) -> tuple[PnLReconciliationJournal, object]:
    path = tmp_path / "pnl.jsonl"
    journal = PnLReconciliationJournal(
        path,
        currency="EUR",
        liability_quantum="0.01",
    )
    journal.record_accepted_order(
        event_id="accepted-1",
        provider_source_id="betfair",
        provider_order_id="order-1",
        side="BACK",
        accepted_stake="10.00",
        accepted_odds="2.10",
    )
    journal.record_settlement_revision(
        event_id="settled-1",
        provider_source_id="betfair",
        provider_order_id="order-1",
        revision_seq=1,
        cumulative_settled_stake="4.00",
        cumulative_realized_pnl="1.25",
    )
    proven = journal.snapshot()
    assert proven.accepted_order_count == 1
    assert proven.settlement_revision_count == 1
    assert proven.realized_pnl == Decimal("1.25")
    assert proven.open_back_stake == Decimal("6.00")
    return journal, proven


def test_failed_reload_preserves_last_proven_faulted_snapshot(tmp_path: Path) -> None:
    """A storage failure must not publish the partially reset replay workspace."""

    journal, proven = _proven_journal(tmp_path)

    with patch(
        "autosport.pnl_reconciliation._read_regular_journal",
        side_effect=OSError("simulated durable read failure"),
    ):
        with pytest.raises(OSError, match="simulated durable read failure"):
            journal.record_accepted_order(
                event_id="accepted-2",
                provider_source_id="betdaq",
                provider_order_id="order-2",
                side="BACK",
                accepted_stake="3.00",
                accepted_odds="2.00",
            )

    assert journal.faulted
    assert journal.snapshot() == proven


def test_durable_replay_value_error_faults_writer_without_losing_proven_snapshot(
    tmp_path: Path,
) -> None:
    """Corrupt durable replay is not equivalent to rejecting a caller payload."""

    journal, proven = _proven_journal(tmp_path)

    with patch(
        "autosport.pnl_reconciliation._read_regular_journal",
        side_effect=ValueError("simulated noncanonical durable record"),
    ):
        with pytest.raises(ValueError, match="simulated noncanonical durable record"):
            journal.record_accepted_order(
                event_id="accepted-2",
                provider_source_id="betdaq",
                provider_order_id="order-2",
                side="BACK",
                accepted_stake="3.00",
                accepted_odds="2.00",
            )

    assert journal.faulted
    assert journal.snapshot() == proven


def test_snapshot_reload_validation_failure_faults_then_publishes_last_proven_state(
    tmp_path: Path,
) -> None:
    journal, proven = _proven_journal(tmp_path)

    with patch(
        "autosport.pnl_reconciliation._read_regular_journal",
        side_effect=ValueError("simulated truncated durable journal"),
    ):
        with pytest.raises(ValueError, match="simulated truncated durable journal"):
            journal.snapshot()

    assert journal.faulted
    # Faulted snapshots never touch the now-untrusted durable tail again.
    assert journal.snapshot() == proven


def test_caller_validation_value_error_does_not_fault_healthy_journal(
    tmp_path: Path,
) -> None:
    journal, proven = _proven_journal(tmp_path)

    with pytest.raises(ValueError, match="positive stake"):
        journal.record_accepted_order(
            event_id="bad-caller-event",
            provider_source_id="betdaq",
            provider_order_id="bad-order",
            side="BACK",
            accepted_stake="0.00",
            accepted_odds="2.00",
        )

    assert not journal.faulted
    assert journal.snapshot() == proven
