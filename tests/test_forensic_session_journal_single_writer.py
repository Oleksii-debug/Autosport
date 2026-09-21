from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from autosport.forensic_session_journal import (
    ForensicSessionJournal,
    JournalError,
    verify_journal,
)


def _clock() -> datetime:
    return datetime(2026, 9, 21, 19, 35, tzinfo=timezone.utc)


def test_second_live_writer_same_path_is_rejected_before_lifecycle_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forensic-session.jsonl"
    first = ForensicSessionJournal(
        path,
        clock=_clock,
        session_id=str(uuid.UUID(int=1)),
    )
    before_second_open = path.read_bytes()

    # A process-local RLock is insufficient here: a second journal instance has
    # a different RLock while targeting the same durable append/hash-chain tip.
    # The second live writer must fail closed before it can mint a false
    # lifecycle.unclean_restart or advance the shared sequence/hash chain.
    with pytest.raises(JournalError):
        ForensicSessionJournal(
            path,
            clock=_clock,
            session_id=str(uuid.UUID(int=2)),
        )

    assert path.read_bytes() == before_second_open

    # The original writer must still own a coherent tip after the rejected open.
    first.append_material("worker.result", {"ok": True})
    first.close()
    records = verify_journal(path)
    assert [record.event_type for record in records] == [
        "lifecycle.startup",
        "worker.result",
        "lifecycle.shutdown",
    ]
    assert [record.seq for record in records] == [1, 2, 3]


def test_single_writer_exclusion_is_released_after_clean_close(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    first = ForensicSessionJournal(
        path,
        clock=_clock,
        session_id=str(uuid.UUID(int=10)),
    )
    first.close()

    second = ForensicSessionJournal(
        path,
        clock=_clock,
        session_id=str(uuid.UUID(int=11)),
    )
    second.close()

    records = verify_journal(path)
    assert [record.event_type for record in records] == [
        "lifecycle.startup",
        "lifecycle.shutdown",
        "lifecycle.startup",
        "lifecycle.shutdown",
    ]
