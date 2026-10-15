from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from autosport.pnl_reconciliation import PnLReconciliationJournal


@pytest.mark.parametrize(
    ("side", "settled", "realized"),
    [
        ("BACK", "4", "-4"),
        ("BACK", "4", "4.4"),
        ("LAY", "4", "-4.4"),
        ("LAY", "4", "4"),
        ("BACK", "10", "-10"),
        ("BACK", "10", "11"),
        ("LAY", "10", "-11"),
        ("LAY", "10", "10"),
    ],
)
def test_settlement_accepts_exact_gross_outcome_bounds(
    tmp_path: Path,
    side: str,
    settled: str,
    realized: str,
) -> None:
    journal = PnLReconciliationJournal(
        tmp_path / f"{side}-{settled}-{realized}.jsonl",
        currency="USD",
    )
    journal.record_accepted_order(
        event_id="accepted",
        provider_source_id="provider",
        provider_order_id="order",
        side=side,
        accepted_stake="10",
        accepted_odds="2.1",
    )

    snapshot = journal.record_settlement_revision(
        event_id="settled",
        provider_source_id="provider",
        provider_order_id="order",
        revision_seq=1,
        cumulative_settled_stake=settled,
        cumulative_realized_pnl=realized,
    )

    assert snapshot.realized_pnl == Decimal(realized)
    assert snapshot.settlement_revision_count == 1
    assert snapshot.positive_authority_verified is False


@pytest.mark.parametrize(
    ("side", "realized"),
    [
        ("BACK", "4.41"),
        ("BACK", "-4.01"),
        ("LAY", "4.01"),
        ("LAY", "-4.41"),
    ],
)
def test_settlement_rejects_mechanically_impossible_gross_pnl_before_publish(
    tmp_path: Path,
    side: str,
    realized: str,
) -> None:
    journal_path = tmp_path / f"impossible-{side}-{realized}.jsonl"
    journal = PnLReconciliationJournal(journal_path, currency="USD")
    proven = journal.record_accepted_order(
        event_id="accepted",
        provider_source_id="provider",
        provider_order_id="order",
        side=side,
        accepted_stake="10",
        accepted_odds="2.1",
    )
    proven_bytes = journal_path.read_bytes()

    with pytest.raises(ValueError, match="outcome bounds"):
        journal.record_settlement_revision(
            event_id="impossible",
            provider_source_id="provider",
            provider_order_id="order",
            revision_seq=1,
            cumulative_settled_stake="4",
            cumulative_realized_pnl=realized,
        )

    assert journal.faulted is False
    assert journal_path.read_bytes() == proven_bytes
    assert journal.snapshot() == proven
