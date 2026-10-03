from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport.pnl_reconciliation import PnLReconciliationJournal


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows open-file sharing prevents deterministic pathname replacement",
)
def test_snapshot_rejects_path_replacement_during_bounded_replay(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "pnl.jsonl"
    journal = PnLReconciliationJournal(journal_path, currency="USD")
    journal.record_accepted_order(
        event_id="event-1",
        provider_source_id="provider",
        provider_order_id="order-1",
        side="BACK",
        accepted_stake="10",
        accepted_odds="2",
    )
    proven = journal.snapshot()
    proven_bytes = journal_path.read_bytes()

    displaced_path = tmp_path / "opened-before-replay-replacement.jsonl"
    foreign_bytes = b"replacement-created-during-replay"
    real_read = os.read
    replaced = False

    def replace_during_read(fd: int, size: int) -> bytes:
        nonlocal replaced
        data = real_read(fd, size)
        if data and not replaced:
            replaced = True
            journal_path.replace(displaced_path)
            journal_path.write_bytes(foreign_bytes)
        return data

    with patch(
        "autosport.pnl_reconciliation.os.read",
        side_effect=replace_during_read,
    ):
        with pytest.raises(ValueError, match="path changed during bounded replay"):
            journal.snapshot()

    assert replaced is True
    assert journal.faulted is True
    assert journal_path.read_bytes() == foreign_bytes
    assert displaced_path.read_bytes() == proven_bytes
    assert journal.snapshot() == proven
