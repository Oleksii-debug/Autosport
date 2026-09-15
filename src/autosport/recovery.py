from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .decision_ledger import DecisionLedgerIntegrityError, JsonlDecisionLedger
from .integrity import sha256_file
from .run_registry import ReconciliationError, RunRegistry
from .run_transaction import RunTransaction, RunTransactionError
from .workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    reconciled_keys: tuple[str, ...]
    aborted_uncommitted_keys: tuple[str, ...]
    unresolved_without_summary: tuple[str, ...]


def reconcile_late_crashes(workspace: str | Path) -> RecoveryReport:
    """Recover transaction-aware runs while excluding any active economic writer."""

    root = Path(workspace)
    registry_path = root / "run_registry.json"

    try:
        with WorkspaceEconomicLock(root):
            try:
                registry_stat = _lstat_or_none(registry_path)
                if registry_stat is None:
                    if _has_durable_run_history(root):
                        raise ReconciliationError(
                            "run registry is missing while durable run history exists"
                        )
                    return RecoveryReport((), (), ())
                if not stat.S_ISREG(registry_stat.st_mode):
                    raise ReconciliationError("run registry path is not a regular file")
                return _reconcile_late_crashes_locked(root, registry_path)
            except ReconciliationError:
                raise
            except (OSError, ValueError) as exc:
                raise ReconciliationError(f"run registry recovery failed: {exc}") from exc
    except WorkspaceEconomicLockError as exc:
        raise ReconciliationError(
            "workspace has an active economic writer; recovery cannot run concurrently"
        ) from exc
    except OSError as exc:
        raise ReconciliationError(f"workspace recovery failed: {exc}") from exc


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _has_durable_run_history(root: Path) -> bool:
    transaction_root = root / RunTransaction.ROOT_NAME
    transaction_stat = _lstat_or_none(transaction_root)
    if transaction_stat is not None:
        if not stat.S_ISDIR(transaction_stat.st_mode) or any(transaction_root.iterdir()):
            return True
    return any(root.glob("run-*.json"))


def _finalize_terminal_transaction_manifests(
    root: Path,
    registry: RunRegistry,
) -> tuple[str, ...]:
    """Finish the registry-completed/manifest-canonical_committed crash state."""

    transaction_root = root / RunTransaction.ROOT_NAME
    transaction_stat = _lstat_or_none(transaction_root)
    if transaction_stat is None:
        return ()
    if not stat.S_ISDIR(transaction_stat.st_mode):
        raise ReconciliationError("transaction root is not a directory")

    try:
        entries = sorted(transaction_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ReconciliationError("transaction history is unreadable") from exc

    finalized: list[str] = []
    for entry in entries:
        entry_stat = _lstat_or_none(entry)
        if entry_stat is None or not stat.S_ISDIR(entry_stat.st_mode):
            raise ReconciliationError("transaction history entry is not a directory")

        try:
            transaction = RunTransaction(root, entry.name)
            manifest = transaction._read_manifest()
        except RunTransactionError as exc:
            raise ReconciliationError(str(exc)) from exc

        phase = manifest.get("phase")
        if phase != "canonical_committed":
            continue

        experiment_key = manifest.get("experiment_key")
        if not isinstance(experiment_key, str) or not experiment_key:
            raise ReconciliationError(
                "canonical committed transaction lacks experiment identity"
            )
        try:
            registry_item = registry.get(experiment_key)
        except (KeyError, ValueError) as exc:
            raise ReconciliationError(
                "canonical committed transaction lacks registry identity"
            ) from exc
        if registry_item.get("status") != "completed":
            raise ReconciliationError(
                "canonical committed transaction lacks completed registry evidence"
            )
        if registry_item.get("run_id") != entry.name:
            raise ReconciliationError(
                "canonical committed transaction registry run_id mismatch"
            )

        try:
            outcome = RunTransaction.recover(
                root,
                run_id=entry.name,
                registry_item=registry_item,
                experiment_key=experiment_key,
            )
            if outcome.disposition != "committed":
                raise ReconciliationError(
                    "terminal registry transaction recovery returned an unsupported disposition"
                )
            transaction.mark_registry_completed()
        except RunTransactionError as exc:
            raise ReconciliationError(str(exc)) from exc
        finalized.append(experiment_key)

    return tuple(finalized)


def _reconcile_late_crashes_locked(root: Path, registry_path: Path) -> RecoveryReport:
    registry = RunRegistry(registry_path)
    paper_book_path = root / "paper_book.json"
    decision_ledger_path = root / "decisions.jsonl"
    reconciled: list[str] = []
    aborted: list[str] = []
    unresolved: list[str] = []

    for key, item in registry.in_progress():
        run_id = str(item["run_id"])
        result_path = root / f"run-{run_id}.json"
        transaction = RunTransaction(root, run_id)

        try:
            if transaction.manifest_path.is_file():
                outcome = RunTransaction.recover(
                    root,
                    run_id=run_id,
                    registry_item=item,
                    experiment_key=key,
                )
                if outcome.disposition == "aborted_uncommitted":
                    book_hash, ledger_hash = _require_recorded_base_state(
                        item,
                        paper_book_path,
                        decision_ledger_path,
                    )
                    registry.abort_uncommitted(
                        key,
                        reason="transaction never reached durable precommit; canonical economic state remained BASE",
                        paper_book_sha256=book_hash,
                        decision_ledger_sha256=ledger_hash,
                    )
                    aborted.append(key)
                    continue

                if outcome.disposition == "committed" and outcome.summary_path is not None:
                    registry.reconcile_completed_summary(key, outcome.summary_path, paper_book_path)
                    transaction.mark_registry_completed()
                    reconciled.append(key)
                    continue

                raise ReconciliationError("transaction recovery returned an unsupported disposition")

            if _has_recorded_base_hashes(item):
                if result_path.exists():
                    raise ReconciliationError(
                        "transaction-aware run has a canonical summary but no transaction manifest"
                    )
                book_hash, ledger_hash = _require_recorded_base_state(
                    item,
                    paper_book_path,
                    decision_ledger_path,
                )
                registry.abort_uncommitted(
                    key,
                    reason="crash occurred before transaction manifest became durable; canonical economic state remained BASE",
                    paper_book_sha256=book_hash,
                    decision_ledger_sha256=ledger_hash,
                )
                aborted.append(key)
                continue

            # Legacy PR #20 recovery path: only a durable schema-v2 summary plus
            # exact current PaperBook hash can prove completion.
            if result_path.is_file():
                registry.reconcile_completed_summary(key, result_path, paper_book_path)
                reconciled.append(key)
            else:
                unresolved.append(key)

        except RunTransactionError as exc:
            raise ReconciliationError(str(exc)) from exc

    for key in _finalize_terminal_transaction_manifests(root, registry):
        if key not in reconciled:
            reconciled.append(key)

    return RecoveryReport(tuple(reconciled), tuple(aborted), tuple(unresolved))


def _has_recorded_base_hashes(item: dict) -> bool:
    return isinstance(item.get("base_paper_book_sha256"), str) and isinstance(
        item.get("base_decision_ledger_sha256"), str
    )


def _require_recorded_base_state(
    item: dict,
    paper_book_path: Path,
    decision_ledger_path: Path,
) -> tuple[str, str]:
    expected_book = item.get("base_paper_book_sha256")
    expected_ledger = item.get("base_decision_ledger_sha256")
    if not isinstance(expected_book, str) or len(expected_book) != 64:
        raise ReconciliationError("registry lacks a valid base PaperBook SHA-256")
    if not isinstance(expected_ledger, str) or len(expected_ledger) != 64:
        raise ReconciliationError("registry lacks a valid base Decision Ledger SHA-256")
    if not paper_book_path.is_file() or not decision_ledger_path.is_file():
        raise ReconciliationError("canonical economic base files are missing")

    actual_book = sha256_file(paper_book_path)
    try:
        ledger_snapshot = JsonlDecisionLedger(decision_ledger_path).verified_snapshot()
    except DecisionLedgerIntegrityError as exc:
        raise ReconciliationError(
            f"canonical Decision Ledger integrity validation failed: {exc}"
        ) from exc
    if actual_book != expected_book or ledger_snapshot.sha256 != expected_ledger:
        raise ReconciliationError("canonical economic state no longer matches the recorded transaction BASE")
    return actual_book, ledger_snapshot.sha256
