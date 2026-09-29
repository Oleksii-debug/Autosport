from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport.pnl_reconciliation import PnLReconciliationJournal


def test_failed_reload_preserves_last_proven_faulted_snapshot(tmp_path: Path) -> None:
    """A storage failure must not publish the partially reset replay workspace."""

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
