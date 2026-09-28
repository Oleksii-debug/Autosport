from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport.pnl_reconciliation import PnLReconciliationJournal


def _record_first(journal: PnLReconciliationJournal):
    return journal.record_accepted_order(
        event_id="accept-1",
        provider_source_id="betfair",
        provider_order_id="order-1",
        side="BACK",
        accepted_stake="10.00",
        accepted_odds="2.00",
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_first_create_fsyncs_file_then_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "pnl.jsonl"
    journal = PnLReconciliationJournal(path, currency="EUR")
    real_fsync = os.fsync
    fsync_kinds: list[str] = []

    def observe(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        fsync_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    with patch("autosport.pnl_reconciliation.os.fsync", side_effect=observe):
        _record_first(journal)

    assert fsync_kinds == ["file", "directory"]

    fsync_kinds.clear()
    with patch("autosport.pnl_reconciliation.os.fsync", side_effect=observe):
        journal.record_settlement_revision(
            event_id="settle-1",
            provider_source_id="betfair",
            provider_order_id="order-1",
            revision_seq=1,
            cumulative_settled_stake="1.00",
            cumulative_realized_pnl="0.50",
        )

    assert fsync_kinds == ["file"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_directory_fsync_failure_blocks_in_memory_publication(tmp_path: Path) -> None:
    path = tmp_path / "pnl.jsonl"
    journal = PnLReconciliationJournal(path, currency="EUR")
    real_fsync = os.fsync

    def fail_directory(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory durability ambiguity")
        real_fsync(fd)

    with patch("autosport.pnl_reconciliation.os.fsync", side_effect=fail_directory):
        with pytest.raises(OSError, match="directory durability ambiguity"):
            _record_first(journal)

    assert journal.faulted
    assert journal.snapshot().accepted_order_count == 0
