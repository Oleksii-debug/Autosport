from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json, ensure_durable_file, sha256_file
from .paper import PaperBook
from .run_registry import RunRegistry
from .run_transaction import RunTransaction


_RUN_ID = "process-kill-relaunch-recovery-audit"
_MARKET_SHA256 = "a" * 64
_RESULTS_SHA256 = "b" * 64
_STRATEGY_ID = "baseline-v1"
_READY_TIMEOUT_SECONDS = 20.0
_CHILD_TIMEOUT_SECONDS = 30.0


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise RuntimeError(f"process-recovery evidence contains duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_nonfinite(value: str) -> None:
    raise RuntimeError(f"process-recovery evidence contains non-finite JSON value: {value}")


def _read_json_object(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read process-recovery evidence: {source}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"process-recovery evidence must be a JSON object: {source}")
    return payload


def _self_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "autosport.windows_entry"]


def _wait_for_ready(process: subprocess.Popen[str], ready_path: Path) -> dict[str, Any]:
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if ready_path.is_file():
            return _read_json_object(ready_path)
        returncode = process.poll()
        if returncode is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(
                f"crash worker exited before durable ready marker: returncode={returncode}; stderr={stderr[-2000:]}"
            )
        time.sleep(0.05)
    raise RuntimeError("timed out waiting for crash worker durable ready marker")


def run_process_recovery_crash_worker(workspace_path: str | Path, ready_path: str | Path) -> int:
    workspace = Path(workspace_path)
    ready = Path(ready_path)
    workspace.mkdir(parents=True, exist_ok=True)
    paper_path = workspace / "paper_book.json"
    ledger_path = workspace / "decisions.jsonl"

    PaperBook("100").save(paper_path)
    ensure_durable_file(paper_path)
    ensure_durable_file(ledger_path)
    paper_hash = sha256_file(paper_path)
    ledger_hash = sha256_file(ledger_path)

    registry = RunRegistry(workspace / "run_registry.json")
    experiment_key = registry.begin(
        _MARKET_SHA256,
        _RESULTS_SHA256,
        _STRATEGY_ID,
        _RUN_ID,
        base_paper_book_sha256=paper_hash,
        base_decision_ledger_sha256=ledger_hash,
    )
    tx = RunTransaction.start(
        workspace,
        run_id=_RUN_ID,
        experiment_key=experiment_key,
        market_sha256=_MARKET_SHA256,
        results_sha256=_RESULTS_SHA256,
        strategy_id=_STRATEGY_ID,
        base_paper_book_sha256=paper_hash,
        base_decision_ledger_sha256=ledger_hash,
    )
    if not tx.manifest_path.is_file():
        raise RuntimeError("crash worker transaction manifest is not durable")

    atomic_write_json(
        ready,
        {
            "status": "READY_FOR_FORCED_PROCESS_KILL",
            "pid": os.getpid(),
            "run_id": _RUN_ID,
            "experiment_key": experiment_key,
            "paper_book_sha256": paper_hash,
            "decision_ledger_sha256": ledger_hash,
            "real_money_execution": False,
        },
    )

    # The parent process must terminate this worker externally after observing the
    # durable marker. Reaching normal return would invalidate the process-kill proof.
    while True:
        time.sleep(60.0)


def run_process_recovery_recover_worker(workspace_path: str | Path, output_path: str | Path) -> int:
    workspace = Path(workspace_path)
    destination = Path(output_path)
    paper_path = workspace / "paper_book.json"
    ledger_path = workspace / "decisions.jsonl"

    try:
        registry = RunRegistry(workspace / "run_registry.json")
        unresolved = registry.in_progress()
        if len(unresolved) != 1:
            raise RuntimeError(f"expected exactly one interrupted run, found {len(unresolved)}")
        experiment_key, registry_item = unresolved[0]
        if registry_item.get("run_id") != _RUN_ID:
            raise RuntimeError("interrupted run identity does not match process-recovery audit")

        recovered = RunTransaction.recover(
            workspace,
            run_id=_RUN_ID,
            registry_item=registry_item,
            experiment_key=experiment_key,
        )
        if recovered.disposition != "aborted_uncommitted":
            raise RuntimeError(
                f"interrupted staging transaction recovered as {recovered.disposition!r}, expected aborted_uncommitted"
            )

        paper_hash = sha256_file(paper_path)
        ledger_hash = sha256_file(ledger_path)
        registry.abort_uncommitted(
            experiment_key,
            reason="real process kill/relaunch recovery audit",
            paper_book_sha256=paper_hash,
            decision_ledger_sha256=ledger_hash,
        )
        if registry.in_progress():
            raise RuntimeError("process recovery left an unresolved registry entry")

        item = registry.get(experiment_key)
        if item.get("status") != "aborted":
            raise RuntimeError("process recovery did not persist aborted registry state")
        manifest_path = RunTransaction(workspace, _RUN_ID).manifest_path
        manifest = _read_json_object(manifest_path)
        if manifest.get("phase") != "aborted":
            raise RuntimeError("process recovery did not persist aborted transaction phase")

        base = manifest.get("base")
        if not isinstance(base, dict):
            raise RuntimeError("transaction manifest lacks canonical base evidence")
        if paper_hash != base.get("paper_book_sha256") or ledger_hash != base.get("decision_ledger_sha256"):
            raise RuntimeError("process recovery changed canonical economic BASE state")

        atomic_write_json(
            destination,
            {
                "status": "PASS",
                "run_id": _RUN_ID,
                "experiment_key": experiment_key,
                "recovery_disposition": recovered.disposition,
                "run_status": item.get("status"),
                "manifest_phase": manifest.get("phase"),
                "paper_book_sha256": paper_hash,
                "decision_ledger_sha256": ledger_hash,
                "economic_base_preserved": True,
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
                "v1_ready": False,
            },
        )
        return 0
    except Exception as exc:
        atomic_write_json(
            destination,
            {
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
                "v1_ready": False,
            },
        )
        return 1


def run_process_recovery_audit(output_path: str | Path) -> int:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    crash_process: subprocess.Popen[str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="autosport-process-recovery-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            ready_path = root / "crash-worker-ready.json"
            recovery_path = root / "recovery-worker.json"

            crash_process = subprocess.Popen(
                _self_command()
                + ["--process-recovery-crash-worker", str(workspace), str(ready_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            ready = _wait_for_ready(crash_process, ready_path)
            if ready.get("status") != "READY_FOR_FORCED_PROCESS_KILL":
                raise RuntimeError("crash worker emitted an invalid ready status")
            if ready.get("pid") != crash_process.pid:
                raise RuntimeError("durable ready marker is not bound to the spawned crash-worker PID")
            if ready.get("real_money_execution") is not False:
                raise RuntimeError("crash worker crossed the paper-only boundary")

            crash_process.kill()
            crash_returncode = crash_process.wait(timeout=_CHILD_TIMEOUT_SECONDS)
            if crash_returncode == 0:
                raise RuntimeError("crash worker exited successfully instead of being forcibly terminated")
            crash_process = None

            recovered_process = subprocess.run(
                _self_command()
                + ["--process-recovery-recover-worker", str(workspace), str(recovery_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=_CHILD_TIMEOUT_SECONDS,
                check=False,
            )
            if recovered_process.returncode != 0:
                recovery = _read_json_object(recovery_path) if recovery_path.is_file() else {}
                raise RuntimeError(
                    "fresh recovery worker failed: "
                    f"returncode={recovered_process.returncode}; "
                    f"evidence={recovery}; stderr={recovered_process.stderr[-2000:]}"
                )

            recovery = _read_json_object(recovery_path)
            if recovery.get("status") != "PASS":
                raise RuntimeError("fresh recovery worker did not produce PASS evidence")
            if recovery.get("recovery_disposition") != "aborted_uncommitted":
                raise RuntimeError("fresh recovery worker did not fail closed on interrupted staging state")
            if recovery.get("run_status") != "aborted" or recovery.get("manifest_phase") != "aborted":
                raise RuntimeError("fresh recovery worker did not persist aborted recovery state")
            if recovery.get("economic_base_preserved") is not True:
                raise RuntimeError("fresh recovery worker did not prove economic BASE preservation")

            registry = RunRegistry(workspace / "run_registry.json")
            if registry.in_progress():
                raise RuntimeError("parent verification found unresolved economic runs after relaunch recovery")
            experiment_key = ready.get("experiment_key")
            if not isinstance(experiment_key, str) or not experiment_key:
                raise RuntimeError("crash worker ready evidence lacks experiment identity")
            registry_item = registry.get(experiment_key)
            if registry_item.get("status") != "aborted":
                raise RuntimeError("parent verification did not observe durable aborted registry state")
            if sha256_file(workspace / "paper_book.json") != ready.get("paper_book_sha256"):
                raise RuntimeError("PaperBook changed across forced process kill/relaunch")
            if sha256_file(workspace / "decisions.jsonl") != ready.get("decision_ledger_sha256"):
                raise RuntimeError("Decision Ledger changed across forced process kill/relaunch")

            atomic_write_json(
                destination,
                {
                    "status": "PASS",
                    "audit_id": "real-process-kill-relaunch-recovery-v1",
                    "forced_process_kill_observed": True,
                    "crash_worker_returncode": crash_returncode,
                    "recovery_worker_returncode": recovered_process.returncode,
                    "run_id": _RUN_ID,
                    "experiment_key": experiment_key,
                    "recovery_disposition": recovery["recovery_disposition"],
                    "run_status": registry_item.get("status"),
                    "manifest_phase": recovery["manifest_phase"],
                    "paper_book_sha256": recovery["paper_book_sha256"],
                    "decision_ledger_sha256": recovery["decision_ledger_sha256"],
                    "economic_base_preserved": True,
                    "real_money_execution": False,
                    "human_tested": False,
                    "nvda_verified": False,
                    "v1_ready": False,
                },
            )
        return 0
    except Exception as exc:
        if crash_process is not None and crash_process.poll() is None:
            crash_process.kill()
            try:
                crash_process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        atomic_write_json(
            destination,
            {
                "status": "FAIL",
                "audit_id": "real-process-kill-relaunch-recovery-v1",
                "error": f"{type(exc).__name__}: {exc}",
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
                "v1_ready": False,
            },
        )
        return 1
