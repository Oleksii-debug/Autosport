from __future__ import annotations

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
    if not registry_path.is_file():
        return RecoveryReport((), (), ())

    try:
        with WorkspaceEconomicLock(root):
            return _reconcile_late_crashes_locked(root, registry_path)
    except WorkspaceEconomicLockError as exc:
        raise ReconciliationError(
            "workspace has an active economic writer; recovery cannot run concurrently"
        ) from exc


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
