from __future__ import annotations

import os
import tempfile
from pathlib import Path

from autosport.process_recovery_audit import audit_process_kill_relaunch


def test_process_kill_relaunch_recovers_uncommitted_transaction_in_fresh_process() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        evidence = audit_process_kill_relaunch(Path(tmp))

    assert evidence["status"] == "PASS"
    assert evidence["stage_pid"] != os.getpid()
    assert evidence["recovery_pid"] != os.getpid()
    assert evidence["stage_pid"] != evidence["recovery_pid"]
    assert evidence["killed_return_code"] != 0
    assert evidence["disposition"] == "aborted_uncommitted"
    assert evidence["registry_status"] == "aborted"
    assert evidence["manifest_phase"] == "aborted"
    assert len(evidence["paper_book_sha256"]) == 64
    assert len(evidence["decision_ledger_sha256"]) == 64
    assert evidence["real_money_execution"] is False
