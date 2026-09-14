from __future__ import annotations

import json
from pathlib import Path

from autosport.process_recovery_audit import run_process_recovery_audit


def test_real_process_kill_and_relaunch_recovers_fail_closed(tmp_path: Path) -> None:
    output = tmp_path / "process-recovery-audit.json"

    assert run_process_recovery_audit(output) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    assert payload["audit_id"] == "real-process-kill-relaunch-recovery-v1"
    assert payload["forced_process_kill_observed"] is True
    assert payload["crash_worker_returncode"] != 0
    assert payload["recovery_worker_returncode"] == 0
    assert payload["recovery_disposition"] == "aborted_uncommitted"
    assert payload["run_status"] == "aborted"
    assert payload["manifest_phase"] == "aborted"
    assert payload["economic_base_preserved"] is True
    assert len(payload["paper_book_sha256"]) == 64
    assert len(payload["decision_ledger_sha256"]) == 64
    assert payload["real_money_execution"] is False
    assert payload["human_tested"] is False
    assert payload["nvda_verified"] is False
    assert payload["v1_ready"] is False
