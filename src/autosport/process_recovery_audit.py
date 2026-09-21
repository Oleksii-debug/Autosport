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

from .decision_ledger import DecisionRecord, JsonlDecisionLedger
from .integrity import atomic_write_json, ensure_durable_file, sha256_file
from .paper import PaperBook
from .restart_recovery_audit import run_restart_recovery_audit
from .run_registry import RunRegistry
from .run_transaction import RunTransaction


_PROCESS_RUN_ID = "packaged-process-kill-recovery-audit"
_PROCESS_MARKET_SHA256 = "c" * 64
_PROCESS_RESULTS_SHA256 = "d" * 64
_PROCESS_STRATEGY_ID = "baseline-v1"
_READY_STATUS = "READY_FOR_PARENT_KILL_AFTER_PRECOMMIT"
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


def _entry_environment() -> dict[str, str] | None:
    """Return a child-only environment for an independent frozen instance."""

    if not getattr(sys, "frozen", False):
        return None
    environment = os.environ.copy()
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def run_process_kill_stage_child(workspace_path: str | Path, ready_path: str | Path) -> int:
    workspace = Path(workspace_path)
    ready = Path(ready_path)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        registry = RunRegistry.initialize_pristine(workspace / "run_registry.json")
        paper_path = workspace / "paper_book.json"
        ledger_path = workspace / "decisions.jsonl"
        PaperBook("100").save(paper_path)
        ensure_durable_file(ledger_path)
        book_hash = sha256_file(paper_path)
        ledger_hash = sha256_file(ledger_path)

        experiment_key = registry.begin(
            _PROCESS_MARKET_SHA256,
            _PROCESS_RESULTS_SHA256,
            _PROCESS_STRATEGY_ID,
            _PROCESS_RUN_ID,
            base_paper_book_sha256=book_hash,
            base_decision_ledger_sha256=ledger_hash,
        )
        transaction = RunTransaction.start(
            workspace,
            run_id=_PROCESS_RUN_ID,
            experiment_key=experiment_key,
            market_sha256=_PROCESS_MARKET_SHA256,
            results_sha256=_PROCESS_RESULTS_SHA256,
            strategy_id=_PROCESS_STRATEGY_ID,
            base_paper_book_sha256=book_hash,
            base_decision_ledger_sha256=ledger_hash,
        )
        # Make both canonical economic artifacts change across the PRECOMMIT.
        # The deterministic ledger canary uses the production append/validation path,
        # so fresh-process recovery must promote a real canonical JSONL decision rather
        # than succeeding because BASE and NEW Decision Ledger bytes happen to match.
        JsonlDecisionLedger(transaction.run_ledger_path).append(
            DecisionRecord(
                replay_run_id=_PROCESS_RUN_ID,
                agent="process-recovery-audit",
                observed_ts="2000-01-01T00:00:00+00:00",
                action="precommit_recovery_canary",
                payload={"paper_only": True, "purpose": "process_recovery_audit"},
                context_hash="e" * 64,
                decision_id="process-recovery-audit-canary",
                recorded_at="2000-01-01T00:00:00+00:00",
            )
        )
        # Cross the durable PRECOMMIT boundary before the parent kills this process.
        # A distinct bankroll value is an audit canary proving recovery promotes NEW
        # rather than merely observing unchanged BASE state.
        transaction.stage_outputs(PaperBook("101"), ledger_path)
        summary = transaction.precommit(
            {
                "schema_version": 2,
                "experiment_key": experiment_key,
                "market_sha256": _PROCESS_MARKET_SHA256,
                "sealed_results_sha256": _PROCESS_RESULTS_SHA256,
                "strategy_id": _PROCESS_STRATEGY_ID,
                "run_id": _PROCESS_RUN_ID,
                "real_money_execution": False,
            }
        )
        new_book_hash = str(summary["paper_book_sha256"])
        new_ledger_hash = str(summary["decision_ledger_sha256"])
        if new_book_hash == book_hash:
            raise RuntimeError("process-kill PRECOMMIT did not stage distinct NEW PaperBook state")
        if new_ledger_hash == ledger_hash:
            raise RuntimeError(
                "process-kill PRECOMMIT did not stage distinct NEW Decision Ledger state"
            )
        atomic_write_json(
            ready,
            {
                "status": _READY_STATUS,
                "pid": os.getpid(),
                "run_id": _PROCESS_RUN_ID,
                "experiment_key": experiment_key,
                "base_paper_book_sha256": book_hash,
                "base_decision_ledger_sha256": ledger_hash,
                "new_paper_book_sha256": new_book_hash,
                "new_decision_ledger_sha256": new_ledger_hash,
                "transaction_phase": "precommitted",
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
        base_book_hash = sha256_file(paper_path)
        base_ledger_hash = sha256_file(ledger_path)
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

        transaction = RunTransaction(workspace, _PROCESS_RUN_ID)
        manifest_before = _decode_strict_json(
            transaction.manifest_path,
            label="process-kill pre-recovery transaction manifest",
        )
        if manifest_before.get("phase") != "precommitted":
            raise RuntimeError("process-kill child was not killed after durable PRECOMMIT")

        recovery = RunTransaction.recover(
            workspace,
            run_id=_PROCESS_RUN_ID,
            registry_item=registry_item,
            experiment_key=experiment_key,
        )
        if recovery.disposition != "committed" or recovery.summary_path is None:
            raise RuntimeError("fresh-process PRECOMMIT recovery did not finish canonical commit")

        new_book_hash = sha256_file(paper_path)
        new_ledger_hash = sha256_file(ledger_path)
        expected_new = manifest_before.get("new")
        if not isinstance(expected_new, dict):
            raise RuntimeError("process-kill PRECOMMIT manifest lacks NEW identity")
        if new_book_hash != expected_new.get("paper_book_sha256"):
            raise RuntimeError("fresh-process recovery did not promote expected NEW PaperBook")
        if new_ledger_hash != expected_new.get("decision_ledger_sha256"):
            raise RuntimeError("fresh-process recovery did not promote expected NEW Decision Ledger")
        if new_book_hash == base_book_hash:
            raise RuntimeError("fresh-process recovery did not cross the durable PaperBook boundary")
        if new_ledger_hash == base_ledger_hash:
            raise RuntimeError("fresh-process recovery did not cross the durable Decision Ledger boundary")

        registry.reconcile_completed_summary(
            experiment_key,
            recovery.summary_path,
            paper_path,
        )
        transaction.mark_registry_completed()
        if registry.in_progress():
            raise RuntimeError("process-kill recovery left an unresolved registry entry")
        final_item = registry.get(experiment_key)
        if final_item.get("status") != "completed":
            raise RuntimeError("process-kill recovery did not persist completed registry state")

        manifest = _decode_strict_json(
            transaction.manifest_path,
            label="process-kill transaction manifest",
        )
        if manifest.get("phase") != "completed":
            raise RuntimeError("process-kill transaction manifest did not persist completed phase")

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
                "base_paper_book_sha256": base_book_hash,
                "base_decision_ledger_sha256": base_ledger_hash,
                "new_paper_book_sha256": new_book_hash,
                "new_decision_ledger_sha256": new_ledger_hash,
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
        env=_entry_environment(),
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
        if ready.get("run_id") != _PROCESS_RUN_ID:
            raise RuntimeError("process-kill READY evidence run_id mismatch")
        experiment_key = ready.get("experiment_key")
        if not isinstance(experiment_key, str) or not experiment_key:
            raise RuntimeError("process-kill READY evidence lacks experiment identity")
        if ready.get("transaction_phase") != "precommitted":
            raise RuntimeError("process-kill READY evidence is not bound to PRECOMMIT")
        if ready.get("real_money_execution") is not False:
            raise RuntimeError("process-kill READY evidence crossed the paper-only boundary")
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
        env=_entry_environment(),
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
    if recovered.get("experiment_key") != experiment_key:
        raise RuntimeError("process-kill recovery experiment identity mismatch")
    if recovered.get("disposition") != "committed":
        raise RuntimeError("process-kill recovery disposition mismatch")
    if recovered.get("registry_status") != "completed":
        raise RuntimeError("process-kill recovery registry state mismatch")
    if recovered.get("manifest_phase") != "completed":
        raise RuntimeError("process-kill recovery manifest phase mismatch")
    if recovered.get("base_paper_book_sha256") != ready.get("base_paper_book_sha256"):
        raise RuntimeError("PaperBook BASE identity changed before fresh-process recovery")
    if recovered.get("base_decision_ledger_sha256") != ready.get("base_decision_ledger_sha256"):
        raise RuntimeError("Decision Ledger BASE identity changed before fresh-process recovery")
    if recovered.get("new_paper_book_sha256") != ready.get("new_paper_book_sha256"):
        raise RuntimeError("fresh-process recovery promoted unexpected PaperBook identity")
    if recovered.get("new_decision_ledger_sha256") != ready.get("new_decision_ledger_sha256"):
        raise RuntimeError("fresh-process recovery promoted unexpected Decision Ledger identity")
    if recovered.get("real_money_execution") is not False:
        raise RuntimeError("process-kill recovery crossed the paper-only boundary")

    # Do not trust only the recovery child's self-reported JSON. Reopen the
    # durable workspace from the parent process and independently prove that the
    # canonical files and registry/transaction terminal states match READY's
    # manifest-bound NEW identity after the fresh recovery process has exited.
    parent_registry = RunRegistry(workspace / "run_registry.json")
    if parent_registry.in_progress():
        raise RuntimeError("parent verification found unresolved run after fresh recovery")
    parent_registry_item = parent_registry.get(experiment_key)
    if parent_registry_item.get("status") != "completed":
        raise RuntimeError("parent verification did not observe completed registry state")
    parent_transaction = RunTransaction(workspace, _PROCESS_RUN_ID)
    parent_manifest = _decode_strict_json(
        parent_transaction.manifest_path,
        label="parent process-kill transaction manifest",
    )
    if parent_manifest.get("phase") != "completed":
        raise RuntimeError("parent verification did not observe completed transaction phase")
    parent_new = parent_manifest.get("new")
    if not isinstance(parent_new, dict):
        raise RuntimeError("parent verification transaction manifest lacks NEW identity")
    actual_new_book_hash = sha256_file(workspace / "paper_book.json")
    actual_new_ledger_hash = sha256_file(workspace / "decisions.jsonl")
    if actual_new_book_hash != ready.get("new_paper_book_sha256"):
        raise RuntimeError("parent verification found unexpected canonical PaperBook identity")
    if actual_new_ledger_hash != ready.get("new_decision_ledger_sha256"):
        raise RuntimeError("parent verification found unexpected canonical Decision Ledger identity")
    if actual_new_book_hash == ready.get("base_paper_book_sha256"):
        raise RuntimeError("parent verification did not observe promoted PaperBook state")
    if actual_new_ledger_hash == ready.get("base_decision_ledger_sha256"):
        raise RuntimeError("parent verification did not observe promoted Decision Ledger state")
    if actual_new_book_hash != parent_new.get("paper_book_sha256"):
        raise RuntimeError("parent verification PaperBook does not match transaction manifest")
    if actual_new_ledger_hash != parent_new.get("decision_ledger_sha256"):
        raise RuntimeError("parent verification Decision Ledger does not match transaction manifest")

    return {
        "status": "PASS",
        "stage_pid": stage_pid,
        "killed_return_code": killed_return_code,
        "recovery_pid": recovery_pid,
        "run_id": _PROCESS_RUN_ID,
        "disposition": recovered["disposition"],
        "registry_status": parent_registry_item["status"],
        "manifest_phase": parent_manifest["phase"],
        "base_paper_book_sha256": recovered["base_paper_book_sha256"],
        "base_decision_ledger_sha256": recovered["base_decision_ledger_sha256"],
        "new_paper_book_sha256": actual_new_book_hash,
        "new_decision_ledger_sha256": actual_new_ledger_hash,
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
                "process_recovery_base_paper_book_sha256": process[
                    "base_paper_book_sha256"
                ],
                "process_recovery_base_decision_ledger_sha256": process[
                    "base_decision_ledger_sha256"
                ],
                "process_recovery_new_paper_book_sha256": process[
                    "new_paper_book_sha256"
                ],
                "process_recovery_new_decision_ledger_sha256": process[
                    "new_decision_ledger_sha256"
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
