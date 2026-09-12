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

    def begin(
        self,
        market_sha256: str,
        results_sha256: str,
        strategy_id: str,
        run_id: str,
        allow_repeat: bool = False,
    ) -> str:
        state = self._read()
        base_identity = self.experiment_identity(market_sha256, results_sha256, strategy_id)
        existing = [item for item in state["runs"].values() if item.get("base_identity") == base_identity]
        unresolved = [item for item in existing if item.get("status") == "in_progress"]
        if unresolved:
            raise UnresolvedExperimentError(
                "An earlier run of this dataset/strategy is unresolved; use a new workspace or repair the unresolved run before replaying."
            )
        completed = [item for item in existing if item.get("status") == "completed"]
        if completed and not allow_repeat:
            raise RepeatedExperimentError(
                "This dataset/strategy already completed in this workspace. Explicit allow_repeat is required for another experiment."
            )
        key = base_identity if not completed else f"{base_identity}:repeat:{run_id}"
        state["runs"][key] = {
            "base_identity": base_identity,
            "run_id": run_id,
            "market_sha256": market_sha256,
            "results_sha256": results_sha256,
            "strategy_id": strategy_id,
            "status": "in_progress",
        }
        self._write(state)
        return key

    def complete(self, key: str, result_path: str | None = None) -> None:
        state = self._read()
        item = state["runs"].get(key)
        if item is None:
            raise KeyError(key)
        if item.get("status") != "in_progress":
            raise ValueError("run is not in progress")
        item["status"] = "completed"
        item["result_path"] = result_path
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
        """Complete only a late-crashed run whose durable summary and current PaperBook prove the same commit."""

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
        self._write(state)

    def _read(self) -> dict:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1 or not isinstance(raw.get("runs"), dict):
            raise ValueError("invalid run registry")
        return raw

    def _write(self, raw: dict) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
