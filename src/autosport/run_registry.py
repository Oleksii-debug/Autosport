from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .integrity import sha256_file


_HEX_DIGITS = frozenset("0123456789abcdef")
_ALLOWED_STATUSES = frozenset({"in_progress", "completed", "aborted"})
_REQUIRED_ENTRY_FIELDS = frozenset(
    {
        "base_identity",
        "run_id",
        "market_sha256",
        "results_sha256",
        "strategy_id",
        "status",
    }
)
_OPTIONAL_ENTRY_FIELDS = frozenset(
    {
        "base_paper_book_sha256",
        "base_decision_ledger_sha256",
        "result_path",
        "paper_book_sha256",
        "decision_ledger_sha256",
        "abort_reason",
        "reconciled_from_summary",
    }
)
_HASH_EVIDENCE_FIELDS = (
    "base_paper_book_sha256",
    "base_decision_ledger_sha256",
    "paper_book_sha256",
    "decision_ledger_sha256",
)
_FINAL_ONLY_FIELDS = frozenset(
    {
        "result_path",
        "paper_book_sha256",
        "decision_ledger_sha256",
        "abort_reason",
        "reconciled_from_summary",
    }
)


def _is_canonical_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _require_canonical_sha256(name: str, value: object) -> str:
    if not _is_canonical_sha256(value):
        raise ValueError(f"{name} must be a canonical SHA-256 hex string")
    return value


def _require_nonempty_string(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


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
        else:
            # Validate recovery/economic truth before a session can use an existing workspace.
            self._read()

    @staticmethod
    def experiment_identity(market_sha256: str, results_sha256: str, strategy_id: str) -> str:
        canonical = f"{market_sha256}|{results_sha256}|{strategy_id}".encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def strategy_ids(self) -> tuple[str, ...]:
        """Return strategy identities already bound to economic runs in this workspace."""
        state = self._read()
        values: set[str] = set()
        for item in state["runs"].values():
            values.add(item["strategy_id"])
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
        _require_canonical_sha256("market_sha256", market_sha256)
        _require_canonical_sha256("results_sha256", results_sha256)
        _require_nonempty_string("strategy_id", strategy_id)
        _require_nonempty_string("run_id", run_id)
        if not isinstance(allow_repeat, bool):
            raise ValueError("allow_repeat must be a boolean")
        if (base_paper_book_sha256 is None) != (base_decision_ledger_sha256 is None):
            raise ValueError("base transaction hashes must be supplied together")
        if base_paper_book_sha256 is not None:
            _require_canonical_sha256("base_paper_book_sha256", base_paper_book_sha256)
            _require_canonical_sha256("base_decision_ledger_sha256", base_decision_ledger_sha256)

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
        if result_path is not None and not isinstance(result_path, str):
            raise ValueError("result_path must be a string or null")
        if paper_book_sha256 is not None:
            _require_canonical_sha256("paper_book_sha256", paper_book_sha256)
        if decision_ledger_sha256 is not None:
            _require_canonical_sha256("decision_ledger_sha256", decision_ledger_sha256)

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
        self._validate_entry(key, item)
        self._write(state)

    def abort_uncommitted(
        self,
        key: str,
        *,
        reason: str,
        paper_book_sha256: str,
        decision_ledger_sha256: str,
    ) -> None:
        if not isinstance(reason, str):
            raise ValueError("abort reason must be a string")
        _require_canonical_sha256("paper_book_sha256", paper_book_sha256)
        _require_canonical_sha256("decision_ledger_sha256", decision_ledger_sha256)

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
        self._validate_entry(key, item)
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
            summary = json.loads(
                result.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ReconciliationError("run summary is unreadable or invalid JSON") from exc
        schema_version = summary.get("schema_version") if isinstance(summary, dict) else None
        if (
            not isinstance(summary, dict)
            or isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 2
        ):
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
        if not _is_canonical_sha256(declared_book_hash):
            raise ReconciliationError("run summary lacks canonical PaperBook SHA-256 reconciliation evidence")
        actual_book_hash = sha256_file(book)
        if actual_book_hash != declared_book_hash:
            raise ReconciliationError("current PaperBook SHA-256 does not match completed run summary")

        declared_ledger_hash = summary.get("decision_ledger_sha256")
        if declared_ledger_hash is not None:
            ledger_path = workspace / "decisions.jsonl"
            if not _is_canonical_sha256(declared_ledger_hash):
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
        self._validate_entry(key, item)
        self._write(state)

    def _validate_entry(self, key: object, item: object) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("run registry contains an invalid experiment key")
        if not isinstance(item, dict):
            raise ValueError("run registry contains an invalid run entry")
        fields = set(item)
        if not _REQUIRED_ENTRY_FIELDS.issubset(fields) or not fields.issubset(
            _REQUIRED_ENTRY_FIELDS | _OPTIONAL_ENTRY_FIELDS
        ):
            raise ValueError("run registry contains invalid run entry fields")

        status = item.get("status")
        if status not in _ALLOWED_STATUSES:
            raise ValueError("run registry contains an invalid status")
        market_sha256 = item.get("market_sha256")
        results_sha256 = item.get("results_sha256")
        strategy_id = item.get("strategy_id")
        run_id = item.get("run_id")
        if not _is_canonical_sha256(market_sha256) or not _is_canonical_sha256(results_sha256):
            raise ValueError("run registry contains invalid canonical SHA-256 identity fields")
        if (
            not isinstance(strategy_id, str)
            or not strategy_id
            or not isinstance(run_id, str)
            or not run_id
        ):
            raise ValueError("run registry contains invalid experiment identity fields")

        base_book_present = "base_paper_book_sha256" in item
        base_ledger_present = "base_decision_ledger_sha256" in item
        if base_book_present != base_ledger_present:
            raise ValueError("run registry contains incomplete base transaction evidence")
        for field_name in _HASH_EVIDENCE_FIELDS:
            if field_name in item and not _is_canonical_sha256(item[field_name]):
                raise ValueError(f"run registry contains invalid {field_name}")

        if "result_path" in item and item["result_path"] is not None and not isinstance(item["result_path"], str):
            raise ValueError("run registry contains an invalid result_path")
        if "abort_reason" in item and not isinstance(item["abort_reason"], str):
            raise ValueError("run registry contains an invalid abort_reason")
        if "reconciled_from_summary" in item and item["reconciled_from_summary"] is not True:
            raise ValueError("run registry contains invalid reconciliation evidence")

        expected_base_identity = self.experiment_identity(
            market_sha256,
            results_sha256,
            strategy_id,
        )
        if item.get("base_identity") != expected_base_identity:
            raise ValueError("run registry contains an inconsistent base identity")
        expected_keys = {
            expected_base_identity,
            f"{expected_base_identity}:repeat:{run_id}",
            f"{expected_base_identity}:retry:{run_id}",
        }
        if key not in expected_keys:
            raise ValueError("run registry contains an inconsistent experiment key")

        if status == "in_progress":
            unexpected = fields & _FINAL_ONLY_FIELDS
            if unexpected:
                raise ValueError("in-progress run registry entry contains final-state evidence")
        elif status == "aborted":
            required_abort_fields = {
                "base_paper_book_sha256",
                "base_decision_ledger_sha256",
                "paper_book_sha256",
                "decision_ledger_sha256",
                "abort_reason",
            }
            if not required_abort_fields.issubset(fields):
                raise ValueError("aborted run registry entry lacks rollback evidence")
            if "result_path" in fields or "reconciled_from_summary" in fields:
                raise ValueError("aborted run registry entry contains completed-run evidence")
            if item["paper_book_sha256"] != item["base_paper_book_sha256"]:
                raise ValueError("aborted run registry PaperBook evidence does not match transaction base")
            if item["decision_ledger_sha256"] != item["base_decision_ledger_sha256"]:
                raise ValueError("aborted run registry Decision Ledger evidence does not match transaction base")
        else:
            if "abort_reason" in fields:
                raise ValueError("completed run registry entry contains abort evidence")
            if item.get("reconciled_from_summary") is True:
                if not isinstance(item.get("result_path"), str):
                    raise ValueError("reconciled run registry entry lacks result_path evidence")
                if "paper_book_sha256" not in fields:
                    raise ValueError("reconciled run registry entry lacks PaperBook hash evidence")

    def _read(self) -> dict:
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid run registry") from exc

        schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "runs"}
            or isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
            or not isinstance(raw.get("runs"), dict)
        ):
            raise ValueError("invalid run registry")
        for key, item in raw["runs"].items():
            self._validate_entry(key, item)
        return raw

    def _write(self, raw: dict) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
