from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import autosport.process_recovery_audit as process_audit
from autosport.process_recovery_audit import audit_process_kill_relaunch


class _BrokenStringError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("diagnostic rendering must not escape")


def test_process_kill_relaunch_recovers_precommit_in_fresh_process() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        evidence = audit_process_kill_relaunch(Path(tmp))

    assert evidence["status"] == "PASS"
    assert evidence["stage_pid"] != os.getpid()
    assert evidence["recovery_pid"] != os.getpid()
    assert evidence["stage_pid"] != evidence["recovery_pid"]
    assert evidence["killed_return_code"] != 0
    assert evidence["disposition"] == "committed"
    assert evidence["registry_status"] == "completed"
    assert evidence["manifest_phase"] == "completed"
    assert len(evidence["base_paper_book_sha256"]) == 64
    assert len(evidence["base_decision_ledger_sha256"]) == 64
    assert len(evidence["new_paper_book_sha256"]) == 64
    assert len(evidence["new_decision_ledger_sha256"]) == 64
    assert evidence["new_paper_book_sha256"] != evidence["base_paper_book_sha256"]
    assert evidence["new_decision_ledger_sha256"] != evidence["base_decision_ledger_sha256"]
    assert evidence["real_money_execution"] is False

def test_packaged_audit_attributes_process_kill_failure(tmp_path, monkeypatch) -> None:
    output = tmp_path / "restart-recovery-audit.json"

    def pass_base_audit(destination) -> int:
        Path(destination).write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "real_money_execution": False,
                    "human_tested": False,
                    "nvda_verified": False,
                }
            ),
            encoding="utf-8",
        )
        return 0

    def fail_process_kill(_root):
        raise ValueError("process-kill-canary")

    monkeypatch.setattr(process_audit, "run_restart_recovery_audit", pass_base_audit)
    monkeypatch.setattr(process_audit, "audit_process_kill_relaunch", fail_process_kill)

    assert process_audit.run_packaged_restart_recovery_audit(output) == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert payload["phase"] == "process_kill_relaunch"
    assert payload["error"] == "ValueError: process-kill-canary"
    assert payload["real_money_execution"] is False
    assert payload["human_tested"] is False
    assert payload["nvda_verified"] is False



def test_stage_child_broken_exception_string_still_publishes_fail_evidence(tmp_path, monkeypatch) -> None:
    ready = tmp_path / "ready.json"

    def fail_initialize(_cls, _path):
        raise _BrokenStringError()

    monkeypatch.setattr(process_audit.RunRegistry, "initialize_pristine", fail_initialize)

    assert process_audit.run_process_kill_stage_child(tmp_path / "workspace", ready) == 1
    payload = json.loads(ready.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert payload["error"] == "_BrokenStringError: exception details unavailable"
    assert payload["real_money_execution"] is False


def test_recovery_child_broken_exception_string_still_publishes_fail_evidence(tmp_path, monkeypatch) -> None:
    output = tmp_path / "recovery.json"

    def fail_hash(_path):
        raise _BrokenStringError()

    monkeypatch.setattr(process_audit, "sha256_file", fail_hash)

    assert process_audit.run_process_kill_recovery_child(tmp_path / "workspace", output) == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert payload["error"] == "_BrokenStringError: exception details unavailable"
    assert payload["real_money_execution"] is False


def test_packaged_audit_broken_exception_string_still_publishes_fail_evidence(tmp_path, monkeypatch) -> None:
    output = tmp_path / "restart-recovery-audit.json"

    def pass_base_audit(destination) -> int:
        Path(destination).write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "real_money_execution": False,
                    "human_tested": False,
                    "nvda_verified": False,
                }
            ),
            encoding="utf-8",
        )
        return 0

    def fail_process_kill(_root):
        raise _BrokenStringError()

    monkeypatch.setattr(process_audit, "run_restart_recovery_audit", pass_base_audit)
    monkeypatch.setattr(process_audit, "audit_process_kill_relaunch", fail_process_kill)

    assert process_audit.run_packaged_restart_recovery_audit(output) == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert payload["phase"] == "process_kill_relaunch"
    assert payload["error"] == "_BrokenStringError: exception details unavailable"
    assert payload["real_money_execution"] is False
    assert payload["human_tested"] is False
    assert payload["nvda_verified"] is False
