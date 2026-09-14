from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json, ensure_durable_file, sha256_file
from .paper import PaperBook
from .restart_recovery_audit import run_restart_recovery_audit
from .run_registry import RunRegistry
from .run_transaction import RunTransaction


_PROCESS_RUN_ID = "packaged-process-kill-recovery-audit"
_PROCESS_MARKET_SHA256 = "c" * 64
_PROCESS_RESULTS_SHA256 = "d" * 64
_PROCESS_STRATEGY_ID = "baseline-v1"
_READY_STATUS = "READY_FOR_PARENT_KILL"
_CHILD_START_TIMEOUT_SECONDS = 60.0
_CHILD_RECOVERY_TIMEOUT_SECONDS = 60.0


def _decode_strict_json(path: Path, *, label: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise RuntimeError(f"{label} contains duplicate JSON key {key!r}")
            payload[key] = value
        return payload

    def reject_non_finite(value: str) -> None:
        raise RuntimeError(f"{label} contains non-finite JSON value {value!r}")

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise RuntimeError(f"{label} is unreadable or invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return payload


def _entry_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "autosport.windows_entry", *args]


def run_process_kill_stage_child(workspace_path: str | Path, ready_path: str | Path) -> int:
    workspace = Path(workspace_path)
    ready = Path(ready_path)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        paper_path = workspace / "paper_book.json"
        ledger_path = workspace / "decisions.jsonl"
        PaperBook("100").save(paper_path)
        ensure_durable_file(ledger_path)
        book_hash = sha256_file(paper_path)
        ledger_hash = sha256_file(ledger_path)

        registry = RunRegistry(workspace / "run_registry.json")
        experiment_key = registry.begin(
            _PROCESS_MARKET_SHA256,
            _PROCESS_RESULTS_SHA256,
            _PROCESS_STRATEGY_ID,
            _PROCESS_RUN_ID,
            base_paper_book_sha256=book_hash,
            base_decision_ledger_sha256=ledger_hash,
        )
        RunTransaction.start(
            workspace,
            run_id=_PROCESS_RUN_ID,
            experiment_key=experiment_key,
            market_sha256=_PROCESS_MARKET_SHA256,
            results_sha256=_PROCESS_RESULTS_SHA256,
            strategy_id=_PROCESS_STRATEGY_ID,
            base_paper_book_sha256=book_hash,
            base_decision_ledger_sha256=ledger_hash,
        )
        atomic_write_json(
            ready,
            {
                "status": _READY_STATUS,
                "pid": os.getpid(),
                "run_id": _PROCESS_RUN_ID,
                "experiment_key": experiment_key,
                "paper_book_sha256": book_hash,
                "decision_ledger_sha256": ledger_hash,
                "real_money_execution": False,
            },
        )

        # The parent intentionally terminates this process after the durable READY
        # marker appears. No normal teardown/recovery path may run in this child.
        while True:
            time.sleep(3600.0)
    except BaseException as exc:
        try:
            atomic_write_json(
                ready,
                {
                    "status": "FAIL",
                    "pid": os.getpid(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "real_money_execution": False,
                },
            )
        except BaseException:
            pass
        return 1


def run_process_kill_recovery_child(
    workspace_path: str | Path,
    output_path: str | Path,
) -> int:
    workspace = Path(workspace_path)
    destination = Path(output_path)
    try:
        paper_path = workspace / "paper_book.json"
        ledger_path = workspace / "decisions.jsonl"
        book_hash = sha256_file(paper_path)
        ledger_hash = sha256_file(ledger_path)
        registry = RunRegistry(workspace / "run_registry.json")
        experiment_key = RunRegistry.experiment_identity(
            _PROCESS_MARKET_SHA256,
            _PROCESS_RESULTS_SHA256,
            _PROCESS_STRATEGY_ID,
        )
        registry_item = registry.get(experiment_key)
        if registry_item.get("run_id") != _PROCESS_RUN_ID:
            raise RuntimeError("process-kill registry run_id mismatch")
        if registry_item.get("status") != "in_progress":
            raise RuntimeError("process-kill registry is not unresolved after crash")

        recovery = RunTransaction.recover(
            workspace,
            run_id=_PROCESS_RUN_ID,
            registry_item=registry_item,
            experiment_key=experiment_key,
        )
        if recovery.disposition != "aborted_uncommitted":
            raise RuntimeError("process-kill recovery did not fail closed as aborted_uncommitted")

        if sha256_file(paper_path) != book_hash or sha256_file(ledger_path) != ledger_hash:
            raise RuntimeError("process-kill recovery changed canonical economic BASE state")

        registry.abort_uncommitted(
            experiment_key,
            reason="fresh-process recovery after intentional parent process kill",
            paper_book_sha256=book_hash,
            decision_ledger_sha256=ledger_hash,
        )
        if registry.in_progress():
            raise RuntimeError("process-kill recovery left an unresolved registry entry")
        final_item = registry.get(experiment_key)
        if final_item.get("status") != "aborted":
            raise RuntimeError("process-kill recovery did not persist aborted registry state")

        manifest = _decode_strict_json(
            RunTransaction(workspace, _PROCESS_RUN_ID).manifest_path,
            label="process-kill transaction manifest",
        )
        if manifest.get("phase") != "aborted":
            raise RuntimeError("process-kill transaction manifest did not persist aborted phase")

        atomic_write_json(
            destination,
            {
                "status": "PASS",
                "pid": os.getpid(),
                "run_id": _PROCESS_RUN_ID,
                "experiment_key": experiment_key,
                "disposition": recovery.disposition,
                "registry_status": final_item["status"],
                "manifest_phase": manifest["phase"],
                "paper_book_sha256": book_hash,
                "decision_ledger_sha256": ledger_hash,
                "real_money_execution": False,
            },
        )
        return 0
    except BaseException as exc:
        atomic_write_json(
            destination,
            {
                "status": "FAIL",
                "pid": os.getpid(),
                "error": f"{type(exc).__name__}: {exc}",
                "real_money_execution": False,
            },
        )
        return 1


def audit_process_kill_relaunch(root: Path) -> dict[str, Any]:
    workspace = root / "process-kill-workspace"
    ready_path = root / "process-kill-ready.json"
    recovery_path = root / "process-kill-recovery.json"

    stage = subprocess.Popen(
        _entry_command(
            "--restart-recovery-stage-child",
            str(workspace),
            str(ready_path),
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + _CHILD_START_TIMEOUT_SECONDS
        while not ready_path.is_file():
            return_code = stage.poll()
            if return_code is not None:
                raise RuntimeError(
                    f"process-kill stage child exited before READY marker with code {return_code}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError("process-kill stage child did not become READY")
            time.sleep(0.05)

        ready = _decode_strict_json(ready_path, label="process-kill READY evidence")
        if ready.get("status") != _READY_STATUS:
            raise RuntimeError(
                "process-kill stage child failed before intentional parent kill: "
                + str(ready.get("error", "unknown child failure"))
            )
        stage_pid = ready.get("pid")
        if isinstance(stage_pid, bool) or not isinstance(stage_pid, int) or stage_pid <= 0:
            raise RuntimeError("process-kill READY evidence has invalid pid")
        if stage_pid == os.getpid():
            raise RuntimeError("process-kill stage did not run in a separate process")

        # In a PyInstaller one-file build Popen.pid may be the bootloader launcher
        # while the Python payload has a distinct PID. Kill the durable payload PID
        # reported by the child rather than assuming launcher/payload identity.
        try:
            os.kill(stage_pid, signal.SIGTERM)
        except OSError as exc:
            raise RuntimeError("parent could not terminate process-kill stage payload") from exc
        killed_return_code = stage.wait(timeout=10.0)
        if killed_return_code == 0:
            raise RuntimeError(
                "process-kill stage exited cleanly after intentional parent termination"
            )
    finally:
        if stage.poll() is None:
            stage.kill()
            stage.wait(timeout=10.0)

    recovery = subprocess.run(
        _entry_command(
            "--restart-recovery-recover-child",
            str(workspace),
            str(recovery_path),
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_CHILD_RECOVERY_TIMEOUT_SECONDS,
        check=False,
    )
    if recovery.returncode != 0:
        detail = ""
        if recovery_path.is_file():
            detail = ": " + str(
                _decode_strict_json(
                    recovery_path,
                    label="process-kill recovery failure evidence",
                ).get("error", "unknown recovery failure")
            )
        raise RuntimeError(
            f"fresh recovery child exited {recovery.returncode}{detail}"
        )

    recovered = _decode_strict_json(
        recovery_path,
        label="process-kill recovery evidence",
    )
    if recovered.get("status") != "PASS":
        raise RuntimeError("fresh recovery child did not report PASS")
    recovery_pid = recovered.get("pid")
    if isinstance(recovery_pid, bool) or not isinstance(recovery_pid, int) or recovery_pid <= 0:
        raise RuntimeError("process-kill recovery evidence has invalid pid")
    if recovery_pid in {os.getpid(), stage_pid}:
        raise RuntimeError("recovery did not execute in a distinct fresh process")
    if recovered.get("run_id") != _PROCESS_RUN_ID:
        raise RuntimeError("process-kill recovery run_id mismatch")
    if recovered.get("disposition") != "aborted_uncommitted":
        raise RuntimeError("process-kill recovery disposition mismatch")
    if recovered.get("registry_status") != "aborted":
        raise RuntimeError("process-kill recovery registry state mismatch")
    if recovered.get("manifest_phase") != "aborted":
        raise RuntimeError("process-kill recovery manifest phase mismatch")
    if recovered.get("paper_book_sha256") != ready.get("paper_book_sha256"):
        raise RuntimeError("PaperBook identity changed across process kill/relaunch")
    if recovered.get("decision_ledger_sha256") != ready.get("decision_ledger_sha256"):
        raise RuntimeError("Decision Ledger identity changed across process kill/relaunch")
    if recovered.get("real_money_execution") is not False:
        raise RuntimeError("process-kill recovery crossed the paper-only boundary")

    return {
        "status": "PASS",
        "stage_pid": stage_pid,
        "killed_return_code": killed_return_code,
        "recovery_pid": recovery_pid,
        "run_id": _PROCESS_RUN_ID,
        "disposition": recovered["disposition"],
        "registry_status": recovered["registry_status"],
        "manifest_phase": recovered["manifest_phase"],
        "paper_book_sha256": recovered["paper_book_sha256"],
        "decision_ledger_sha256": recovered["decision_ledger_sha256"],
        "real_money_execution": False,
    }


def run_packaged_restart_recovery_audit(output_path: str | Path) -> int:
    destination = Path(output_path)
    if run_restart_recovery_audit(destination) != 0:
        return 1

    try:
        existing = _decode_strict_json(
            destination,
            label="packaged restart/recovery evidence",
        )
        if existing.get("status") != "PASS":
            raise RuntimeError("base restart/recovery audit did not report PASS")
        with tempfile.TemporaryDirectory() as tmp:
            process = audit_process_kill_relaunch(Path(tmp))
        existing.update(
            {
                "process_kill_relaunch_status": process["status"],
                "process_kill_stage_pid": process["stage_pid"],
                "process_kill_return_code": process["killed_return_code"],
                "process_recovery_pid": process["recovery_pid"],
                "process_recovery_run_id": process["run_id"],
                "process_recovery_disposition": process["disposition"],
                "process_recovery_registry_status": process["registry_status"],
                "process_recovery_manifest_phase": process["manifest_phase"],
                "process_recovery_paper_book_sha256": process["paper_book_sha256"],
                "process_recovery_decision_ledger_sha256": process[
                    "decision_ledger_sha256"
                ],
            }
        )
        if (
            existing.get("real_money_execution") is not False
            or existing.get("human_tested") is not False
            or existing.get("nvda_verified") is not False
        ):
            raise RuntimeError("packaged restart/recovery truth labels are invalid")
        atomic_write_json(destination, existing)
        return 0
    except BaseException as exc:
        atomic_write_json(
            destination,
            {
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
            },
        )
        return 1
