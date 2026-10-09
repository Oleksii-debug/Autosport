from __future__ import annotations

import json
import os
from decimal import Context, Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport.pnl_reconciliation import PnLReconciliationJournal


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "pnl.jsonl"


def _journal(path: Path, **kwargs) -> PnLReconciliationJournal:
    return PnLReconciliationJournal(path, currency="EUR", liability_quantum="0.01", **kwargs)


def _accept(journal, *, event_id="a1", source="betfair", order="o1", side="BACK", stake="10.00", odds="2.10"):
    return journal.record_accepted_order(event_id=event_id, provider_source_id=source, provider_order_id=order, side=side, accepted_stake=stake, accepted_odds=odds)


def _settle(journal, *, event_id="s1", source="betfair", order="o1", seq=1, settled="4.00", pnl="1.25"):
    return journal.record_settlement_revision(event_id=event_id, provider_source_id=source, provider_order_id=order, revision_seq=seq, cumulative_settled_stake=settled, cumulative_realized_pnl=pnl)


def test_empty_truth_withholds_mark_to_market(journal_path):
    s = _journal(journal_path).snapshot()
    assert (s.accepted_order_count, s.realized_pnl, s.open_back_stake, s.open_lay_liability) == (0, Decimal("0"), Decimal("0"), Decimal("0"))
    assert s.mark_to_market_unrealized_pnl is None


def test_back_and_lay_open_exposure_are_conservative(journal_path):
    j = _journal(journal_path)
    _accept(j, stake="12.50")
    s = _accept(j, event_id="a2", source="betdaq", order="o2", side="LAY", stake="1.01", odds="2.015")
    assert s.open_back_stake == Decimal("12.50")
    assert s.open_lay_liability == Decimal("1.03")
    assert s.mark_to_market_unrealized_pnl is None


def test_cumulative_revision_replaces_not_double_counts(journal_path):
    j = _journal(journal_path)
    _accept(j)
    first = _settle(j, settled="4.00", pnl="1.25")
    second = _settle(j, event_id="s2", seq=2, settled="7.00", pnl="2.00")
    assert first.realized_pnl == Decimal("1.25")
    assert second.realized_pnl == Decimal("2.00")
    assert second.open_back_stake == Decimal("3.00")
    assert second.settlement_revision_count == 2


def test_event_replay_is_idempotent_but_conflict_fails(journal_path):
    j = _journal(journal_path)
    expected = _accept(j)
    before = journal_path.read_bytes()
    assert _accept(j) == expected
    assert journal_path.read_bytes() == before
    with pytest.raises(ValueError, match="event_id already exists"):
        _accept(j, stake="11.00")
    assert journal_path.read_bytes() == before


def test_provider_order_identity_is_scoped_and_unique(journal_path):
    j = _journal(journal_path)
    _accept(j, order="42")
    with pytest.raises(ValueError, match="already has accepted-order"):
        _accept(j, event_id="a2", order="42")
    s = _accept(j, event_id="a3", source="betdaq", order="42", stake="5.00")
    assert s.accepted_order_count == 2


def test_revision_requires_known_order_exact_sequence_and_monotonic_stake(journal_path):
    j = _journal(journal_path)
    with pytest.raises(ValueError, match="unknown provider order"):
        _settle(j)
    _accept(j)
    with pytest.raises(ValueError, match="advance by exactly one"):
        _settle(j, seq=2)
    _settle(j, settled="5.00")
    with pytest.raises(ValueError, match="must not move backward"):
        _settle(j, event_id="s2", seq=2, settled="4.99")
    with pytest.raises(ValueError, match="exceeds accepted_stake"):
        _settle(j, event_id="s3", seq=2, settled="10.01")


def test_restart_replays_same_state_and_exact_duplicate_line_once(journal_path):
    j = _journal(journal_path)
    _accept(j, side="LAY", stake="10.00", odds="3.00")
    expected = _settle(j, settled="4.00", pnl="-1.50")
    reopened = _journal(journal_path)
    assert reopened.snapshot() == expected
    line_set = journal_path.read_bytes()
    journal_path.write_bytes(line_set + line_set.splitlines(keepends=True)[-1])
    assert _journal(journal_path).snapshot() == expected


def test_truncated_noncanonical_and_conflicting_journal_fail_closed(journal_path):
    j = _journal(journal_path)
    _accept(j)
    canonical = journal_path.read_bytes()
    journal_path.write_bytes(canonical.rstrip(b"\n"))
    with pytest.raises(ValueError, match="truncated final record"):
        _journal(journal_path)
    event = json.loads(canonical)
    journal_path.write_text(json.dumps(event, separators=(", ", ": ")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not canonical JSON"):
        _journal(journal_path)
    journal_path.write_bytes(canonical)
    event["accepted_stake"] = "11.00"
    journal_path.write_bytes(canonical + (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode())
    with pytest.raises(ValueError, match="reuses event_id"):
        _journal(journal_path)


def test_persist_before_memory_and_ambiguous_tail_reconciles_on_reopen(journal_path):
    j = _journal(journal_path)
    real_fsync = os.fsync
    calls = 0
    def fail_once(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("durability ambiguity")
        real_fsync(fd)
    with patch("autosport.pnl_reconciliation.os.fsync", side_effect=fail_once):
        with pytest.raises(OSError, match="durability ambiguity"):
            _accept(j)
    assert j.snapshot().accepted_order_count == 0
    assert j.faulted
    reopened = _journal(journal_path)
    before = journal_path.read_bytes()
    assert _accept(reopened).accepted_order_count == 1
    assert journal_path.read_bytes() == before


def test_partial_write_completes_and_reopens(journal_path):
    j = _journal(journal_path)
    real_write = os.write
    calls = 0
    def partial(fd, data):
        nonlocal calls
        calls += 1
        return real_write(fd, data[:7] if calls == 1 and len(data) > 7 else data)
    with patch("autosport.pnl_reconciliation.os.write", side_effect=partial):
        expected = _accept(j)
    assert calls >= 2
    assert _journal(journal_path).snapshot() == expected


def test_ambient_decimal_context_cannot_change_economics(journal_path):
    j = _journal(journal_path)
    with localcontext(Context(prec=3)):
        _accept(j, side="LAY", stake="123.456", odds="9.876")
        s = _settle(j, settled="23.456", pnl="-12.3456")
    assert s.realized_pnl == Decimal("-12.3456")
    assert s.open_lay_liability == Decimal("887.60")


def test_scope_identity_types_and_decimal_bounds_fail_closed(journal_path):
    j = _journal(journal_path)
    with pytest.raises(ValueError, match="event_id must be"):
        j.record_accepted_order(event_id=1, provider_source_id="p", provider_order_id="o", side="BACK", accepted_stake="1", accepted_odds="2")
    with pytest.raises(ValueError, match="serialized as a string"):
        j.record_accepted_order(event_id="a", provider_source_id="p", provider_order_id="o", side="BACK", accepted_stake="1", accepted_odds=2)
    with pytest.raises(ValueError, match="supported bounds"):
        _accept(j, odds="1E+1000000")
    assert not journal_path.exists()


def test_scope_configuration_is_bound_across_restart(journal_path):
    j = _journal(journal_path)
    _accept(j)
    with pytest.raises(ValueError, match="currency does not match"):
        PnLReconciliationJournal(journal_path, currency="USD")
    with pytest.raises(ValueError, match="liability_quantum does not match"):
        PnLReconciliationJournal(journal_path, currency="EUR", liability_quantum="0.001")


def test_long_lived_snapshot_refreshes_under_lock_after_other_writer(journal_path):
    first = _journal(journal_path)
    second = _journal(journal_path)
    _accept(first, stake="7.00")

    refreshed = second.snapshot()

    assert refreshed.accepted_order_count == 1
    assert refreshed.open_back_stake == Decimal("7.00")
