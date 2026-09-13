from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .integrity import sha256_file


class RepeatedExperimentError(RuntimeError):
    pass


class UnresolvedExperimentError(RuntimeError):
    pass


class ReconciliationError(RuntimeError):
    pass


class MixedStrategyWorkspaceError(RuntimeError):
    pass


class RunRegistry:
    """Fail-closed experiment ledger preventing accidental replay duplication after restart/crash."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"schema_version": 1, "runs": {}})

    @staticmethod
    def experiment_identity(market_sha256: str, results_sha256: str, strategy_id: str) -> str:
        canonical = f"{market_sha256}|{results_sha256}|{strategy_id}".encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def strategy_ids(self) -> tuple[str, ...]:
        """Return strategy identities already bound to economic runs in this workspace."""
        state = self._read()
        values: set[str] = set()
        for item in state["runs"].values():
            strategy_id = item.get("strategy_id")
            if not isinstance(strategy_id, str) or not strategy_id:
                raise ValueError("run registry contains an invalid strategy_id")
            values.add(strategy_id)
        return tuple(sorted(values))

    def begin(
        self,
        market_sha256: str,
        results_sha256: str,
        strategy_id: str,
        run_id: str,
        allow_repeat: bool = False,
        *,
        base_paper_book_sha256: str | None = None,
        base_decision_ledger_sha256: str | None = None,
    ) -> str:
        if (base_paper_book_sha256 is None) != (base_decision_ledger_sha256 is None):
            raise ValueError("base transaction hashes must be supplied together")
        for value in (base_paper_book_sha256, base_decision_ledger_sha256):
            if value is not None and (not isinstance(value, str) or len(value) != 64):
                raise ValueError("base transaction hashes must be SHA-256 hex strings")

        state = self._read()
        base_identity = self.experiment_identity(market_sha256, results_sha256, strategy_id)
        existing_pairs = [
            (key, item)
            for key, item in state["runs"].items()
            if item.get("base_identity") == base_identity
        ]
        unresolved = [
            item
            for item in state["runs"].values()
            if item.get("status") == "in_progress"
        ]
        if unresolved:
            raise UnresolvedExperimentError(
                "Workspace has an unresolved economic run; repair it before starting another paper experiment."
            )
        completed = [item for _key, item in existing_pairs if item.get("status") == "completed"]
        if completed and not allow_repeat:
            raise RepeatedExperimentError(
                "This dataset/strategy already completed in this workspace. Explicit allow_repeat is required for another experiment."
            )

        if not existing_pairs:
            key = base_identity
        elif completed:
            key = f"{base_identity}:repeat:{run_id}"
        else:
            key = f"{base_identity}:retry:{run_id}"

        entry = {
            "base_identity": base_identity,
            "run_id": run_id,
            "market_sha256": market_sha256,
            "results_sha256": results_sha256,
            "strategy_id": strategy_id,
            "status": "in_progress",
        }
        if base_paper_book_sha256 is not None:
            entry["base_paper_book_sha256"] = base_paper_book_sha256
            entry["base_decision_ledger_sha256"] = base_decision_ledger_sha256
        state["runs"][key] = entry
        self._write(state)
        return key

    def complete(
        self,
        key: str,
        result_path: str | None = None,
        *,
        paper_book_sha256: str | None = None,
        decision_ledger_sha256: str | None = None,
    ) -> None:
        state = self._read()
        item = state["runs"].get(key)
        if item is None:
            raise KeyError(key)
        if item.get("status") != "in_progress":
            raise ValueError("run is not in progress")
        item["status"] = "completed"
        item["result_path"] = result_path
        if paper_book_sha256 is not None:
            item["paper_book_sha256"] = paper_book_sha256
        if decision_ledger_sha256 is not None:
            item["decision_ledger_sha256"] = decision_ledger_sha256
        self._write(state)

    def abort_uncommitted(
        self,
        key: str,
        *,
        reason: str,
        paper_book_sha256: str,
        decision_ledger_sha256: str,
    ) -> None:
        state = self._read()
        item = state["runs"].get(key)
        if item is None:
            raise KeyError(key)
        if item.get("status") != "in_progress":
            raise ReconciliationError("only an in-progress run can be aborted")
        expected_book = item.get("base_paper_book_sha256")
        expected_ledger = item.get("base_decision_ledger_sha256")
        if not isinstance(expected_book, str) or not isinstance(expected_ledger, str):
            raise ReconciliationError("registry lacks base hashes required for safe uncommitted abort")
        if paper_book_sha256 != expected_book or decision_ledger_sha256 != expected_ledger:
            raise ReconciliationError("canonical economic state does not match the recorded transaction base")
        item["status"] = "aborted"
        item["abort_reason"] = reason
        item["paper_book_sha256"] = paper_book_sha256
        item["decision_ledger_sha256"] = decision_ledger_sha256
        self._write(state)

    def in_progress(self) -> tuple[tuple[str, dict], ...]:
        state = self._read()
        return tuple(
            (key, dict(item))
            for key, item in state["runs"].items()
            if item.get("status") == "in_progress"
        )

    def get(self, key: str) -> dict:
        item = self._read()["runs"].get(key)
        if item is None:
            raise KeyError(key)
        return dict(item)

    def reconcile_completed_summary(
        self,
        key: str,
        result_path: str | Path,
        paper_book_path: str | Path,
    ) -> None:
        """Complete only a run whose durable summary and current PaperBook prove the same commit."""

        state = self._read()
        item = state["runs"].get(key)
        if item is None:
            raise KeyError(key)
        if item.get("status") != "in_progress":
            raise ReconciliationError("only an in-progress run can be reconciled")

        result = Path(result_path)
        book = Path(paper_book_path)
        workspace = self.path.parent.resolve()
        if result.resolve().parent != workspace:
            raise ReconciliationError("run summary must be inside the registry workspace")
        if book.resolve() != (workspace / "paper_book.json").resolve():
            raise ReconciliationError("PaperBook reconciliation path must be the canonical workspace paper_book.json")
        expected_name = f"run-{item['run_id']}.json"
        if result.name != expected_name:
            raise ReconciliationError("run summary filename does not match registry run_id")
        if not result.is_file():
            raise ReconciliationError("durable run summary not found")
        if not book.is_file():
            raise ReconciliationError("canonical PaperBook snapshot not found")

        try:
            summary = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReconciliationError("run summary is unreadable or invalid JSON") from exc
        if not isinstance(summary, dict) or summary.get("schema_version") != 2:
            raise ReconciliationError("run summary lacks reconciliation schema version 2")

        expected = {
            "experiment_key": key,
            "run_id": item.get("run_id"),
            "market_sha256": item.get("market_sha256"),
            "sealed_results_sha256": item.get("results_sha256"),
            "strategy_id": item.get("strategy_id"),
        }
        mismatches = [
            field
            for field, value in expected.items()
            if summary.get(field) != value
        ]
        if mismatches:
            raise ReconciliationError(
                "run summary identity mismatch: " + ",".join(sorted(mismatches))
            )
        if summary.get("real_money_execution") is not False:
            raise ReconciliationError("run summary truth boundary is invalid")
        declared_book_hash = summary.get("paper_book_sha256")
        if not isinstance(declared_book_hash, str) or len(declared_book_hash) != 64:
            raise ReconciliationError("run summary lacks PaperBook SHA-256 reconciliation evidence")
        actual_book_hash = sha256_file(book)
        if actual_book_hash != declared_book_hash:
            raise ReconciliationError("current PaperBook SHA-256 does not match completed run summary")

        declared_ledger_hash = summary.get("decision_ledger_sha256")
        if declared_ledger_hash is not None:
            ledger_path = workspace / "decisions.jsonl"
            if not isinstance(declared_ledger_hash, str) or len(declared_ledger_hash) != 64:
                raise ReconciliationError("run summary Decision Ledger SHA-256 evidence is invalid")
            if not ledger_path.is_file() or sha256_file(ledger_path) != declared_ledger_hash:
                raise ReconciliationError("current Decision Ledger SHA-256 does not match completed run summary")

        base_identity = self.experiment_identity(
            str(item.get("market_sha256")),
            str(item.get("results_sha256")),
            str(item.get("strategy_id")),
        )
        if item.get("base_identity") != base_identity:
            raise ReconciliationError("registry base identity is inconsistent")

        item["status"] = "completed"
        item["result_path"] = str(result)
        item["reconciled_from_summary"] = True
        item["paper_book_sha256"] = actual_book_hash
        if declared_ledger_hash is not None:
            item["decision_ledger_sha256"] = declared_ledger_hash
        self._write(state)

    def _read(self) -> dict:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1 or not isinstance(raw.get("runs"), dict):
            raise ValueError("invalid run registry")
        for item in raw["runs"].values():
            if not isinstance(item, dict):
                raise ValueError("run registry contains an invalid run entry")
            if item.get("status") not in {"in_progress", "completed", "aborted"}:
                raise ValueError("run registry contains an invalid status")
            market_sha256 = item.get("market_sha256")
            results_sha256 = item.get("results_sha256")
            strategy_id = item.get("strategy_id")
            run_id = item.get("run_id")
            if (
                not isinstance(market_sha256, str)
                or not market_sha256
                or not isinstance(results_sha256, str)
                or not results_sha256
                or not isinstance(strategy_id, str)
                or not strategy_id
                or not isinstance(run_id, str)
                or not run_id
            ):
                raise ValueError("run registry contains invalid experiment identity fields")
            expected_base_identity = self.experiment_identity(
                market_sha256,
                results_sha256,
                strategy_id,
            )
            if item.get("base_identity") != expected_base_identity:
                raise ValueError("run registry contains an inconsistent base identity")
        return raw

    def _write(self, raw: dict) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
