from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

import autosport.forensic_session_journal as module
from autosport.forensic_session_journal import ForensicSessionJournal, JournalLockedError


def test_fresh_sidecar_is_locked_before_canonical_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical sidecar must never be visible before its OS lease is held."""
    if os.name == "nt":
        pytest.skip("POSIX advisory-lock publication ordering regression")

    path = tmp_path / "forensic-session.jsonl"
    real_link = module.os.link
    observed = {"probed": False}

    def link_and_probe(source: object, destination: object) -> None:
        real_link(source, destination)
        probe = os.open(destination, os.O_RDWR)
        try:
            with pytest.raises(JournalLockedError):
                module._acquire_process_lock(probe)
            observed["probed"] = True
        finally:
            os.close(probe)

    monkeypatch.setattr(module.os, "link", link_and_probe)
    journal = ForensicSessionJournal(
        path,
        clock=lambda: datetime(2026, 9, 21, 18, 55, tzinfo=timezone.utc),
        session_id=str(uuid.UUID(int=56)),
    )
    journal.close()
    assert observed["probed"] is True

@pytest.mark.skipif(os.name != "nt", reason="Windows staging-file sharing regression")
def test_windows_lock_publication_has_no_locked_staging_alias(
    tmp_path: Path,
) -> None:
    """Win32 must not unlink an open locked temp alias during first startup."""

    path = tmp_path / "forensic-session.jsonl"
    journal = ForensicSessionJournal(
        path,
        clock=lambda: datetime(2026, 9, 21, 18, 55, tzinfo=timezone.utc),
        session_id=str(uuid.UUID(int=57)),
    )
    assert not list(tmp_path.glob("*.claim"))
    with pytest.raises(JournalLockedError):
        ForensicSessionJournal(path, session_id=str(uuid.UUID(int=58)))
    journal.close()
    reopened = ForensicSessionJournal(
        path,
        clock=lambda: datetime(2026, 9, 21, 18, 56, tzinfo=timezone.utc),
        session_id=str(uuid.UUID(int=59)),
    )
    reopened.close()
    assert len(module.verify_journal(path)) == 4
