from __future__ import annotations

from pathlib import Path

import pytest

import autosport.pnl_reconciliation as reconciliation


def _accept(
    journal: reconciliation.PnLReconciliationJournal,
    *,
    event_id: str,
    order_id: str,
) -> reconciliation.PnLReconciliationSnapshot:
    return journal.record_accepted_order(
        event_id=event_id,
        provider_source_id="betfair",
        provider_order_id=order_id,
        side="BACK",
        accepted_stake="10.00",
        accepted_odds="2.10",
    )


def test_append_that_would_cross_replay_bound_is_rejected_before_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "pnl.jsonl"
    journal = reconciliation.PnLReconciliationJournal(path, currency="EUR")
    expected = _accept(journal, event_id="accepted-1", order_id="order-1")
    before = path.read_bytes()

    # Keep the already-proven file replayable but leave too little room for another
    # canonical event. Production must reject before writing any bytes that its own
    # bounded replay path would reject on the next reload/restart.
    monkeypatch.setattr(reconciliation, "_MAX_FILE_BYTES", len(before) + 1)

    with pytest.raises(ValueError, match="append exceeds bounded replay size"):
        _accept(journal, event_id="accepted-2", order_id="order-2")

    assert path.read_bytes() == before
    assert journal.faulted is False
    assert journal.snapshot() == expected
    assert (
        reconciliation.PnLReconciliationJournal(path, currency="EUR").snapshot()
        == expected
    )
