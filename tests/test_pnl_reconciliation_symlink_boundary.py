from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport.pnl_reconciliation import PnLReconciliationJournal


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks unavailable in this environment: {exc}")


def test_constructor_never_replays_through_journal_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "external.txt"
    victim.write_bytes(b"external-data-must-not-be-parsed")
    journal_path = tmp_path / "pnl.jsonl"
    _symlink_or_skip(journal_path, victim)

    with pytest.raises(ValueError, match="must not be a symlink"):
        PnLReconciliationJournal(journal_path, currency="USD")

    assert victim.read_bytes() == b"external-data-must-not-be-parsed"


def test_existing_writer_never_appends_through_late_journal_symlink(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "pnl.jsonl"
    journal = PnLReconciliationJournal(journal_path, currency="USD")
    victim = tmp_path / "external.txt"
    victim.write_bytes(b"do-not-touch")
    _symlink_or_skip(journal_path, victim)

    with pytest.raises(ValueError, match="must not be a symlink"):
        journal.record_accepted_order(
            event_id="event-1",
            provider_source_id="provider",
            provider_order_id="order-1",
            side="BACK",
            accepted_stake="10",
            accepted_odds="2",
        )

    assert victim.read_bytes() == b"do-not-touch"
    assert not victim.read_bytes().endswith(b"\n")


def test_non_regular_journal_path_fails_closed(tmp_path: Path) -> None:
    journal_path = tmp_path / "pnl.jsonl"
    journal_path.mkdir()

    with pytest.raises(ValueError, match="must be a regular file"):
        PnLReconciliationJournal(journal_path, currency="USD")


def test_first_append_fails_if_regular_journal_appears_after_empty_replay(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "pnl.jsonl"
    journal = PnLReconciliationJournal(journal_path, currency="USD")
    original_append = journal._append
    foreign_bytes = b"foreign-regular-file-must-not-be-appended"

    def appear_then_append(encoded: bytes) -> None:
        journal_path.write_bytes(foreign_bytes)
        original_append(encoded)

    with patch.object(journal, "_append", side_effect=appear_then_append):
        with pytest.raises(ValueError, match="appeared after replay"):
            journal.record_accepted_order(
                event_id="event-race",
                provider_source_id="provider",
                provider_order_id="order-race",
                side="BACK",
                accepted_stake="10",
                accepted_odds="2",
            )

    assert journal_path.read_bytes() == foreign_bytes
    assert journal.faulted is True
    assert journal.snapshot().accepted_order_count == 0


def test_append_fails_if_replayed_regular_journal_is_replaced_before_open(
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
    replayed_bytes = journal_path.read_bytes()
    displaced_path = tmp_path / "replayed-before-replacement.jsonl"
    foreign_bytes = b"replacement-regular-file-must-not-be-appended"
    original_append = journal._append

    def replace_then_append(encoded: bytes) -> None:
        journal_path.replace(displaced_path)
        journal_path.write_bytes(foreign_bytes)
        original_append(encoded)

    with patch.object(journal, "_append", side_effect=replace_then_append):
        with pytest.raises(ValueError, match="changed after replay"):
            journal.record_accepted_order(
                event_id="event-2",
                provider_source_id="provider",
                provider_order_id="order-2",
                side="BACK",
                accepted_stake="5",
                accepted_odds="3",
            )

    assert displaced_path.read_bytes() == replayed_bytes
    assert journal_path.read_bytes() == foreign_bytes
    assert journal.faulted is True
    loaded = journal.snapshot()
    assert loaded.accepted_order_count == 1
    assert loaded.open_back_stake == 10


def test_append_fails_if_same_regular_journal_changes_after_replay(
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
    original_append = journal._append
    external_tail = b"external-concurrent-tail"

    def mutate_then_append(encoded: bytes) -> None:
        with journal_path.open("ab") as handle:
            handle.write(external_tail)
        original_append(encoded)

    with patch.object(journal, "_append", side_effect=mutate_then_append):
        with pytest.raises(ValueError, match="changed after replay"):
            journal.record_accepted_order(
                event_id="event-2",
                provider_source_id="provider",
                provider_order_id="order-2",
                side="BACK",
                accepted_stake="5",
                accepted_odds="3",
            )

    assert journal_path.read_bytes().endswith(external_tail)
    assert journal.faulted is True
    loaded = journal.snapshot()
    assert loaded.accepted_order_count == 1
    assert loaded.open_back_stake == 10


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows open-file sharing already prevents deterministic pathname replacement",
)
def test_append_fails_if_path_is_replaced_after_exact_journal_open(
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
    replayed_bytes = journal_path.read_bytes()
    displaced_path = tmp_path / "opened-before-replacement.jsonl"
    foreign_bytes = b"replacement-created-after-open"
    real_write = os.write
    replaced = False

    def replace_during_write(fd: int, data: bytes) -> int:
        nonlocal replaced
        if not replaced:
            replaced = True
            journal_path.replace(displaced_path)
            journal_path.write_bytes(foreign_bytes)
        return real_write(fd, data)

    with patch(
        "autosport.pnl_reconciliation.os.write",
        side_effect=replace_during_write,
    ):
        with pytest.raises(ValueError, match="path changed during append"):
            journal.record_accepted_order(
                event_id="event-2",
                provider_source_id="provider",
                provider_order_id="order-2",
                side="BACK",
                accepted_stake="5",
                accepted_odds="3",
            )

    assert replaced is True
    assert journal_path.read_bytes() == foreign_bytes
    assert displaced_path.read_bytes().startswith(replayed_bytes)
    assert displaced_path.read_bytes() != replayed_bytes
    assert journal.faulted is True
    loaded = journal.snapshot()
    assert loaded.accepted_order_count == 1
    assert loaded.open_back_stake == 10
