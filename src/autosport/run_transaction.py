from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .decision_ledger import (
    DecisionLedgerIntegrityError,
    JsonlDecisionLedger,
    VerifiedDecisionLedgerSnapshot,
)
from .integrity import atomic_write_json, ensure_durable_file, sha256_file
from .paper import PaperBook


class RunTransactionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TransactionRecovery:
    disposition: str
    summary_path: Path | None = None


class RunTransaction:
    """Crash-recoverable commit protocol for PaperBook, Decision Ledger and run summary."""

    SCHEMA_VERSION = 1
    ROOT_NAME = ".run-transactions"

    def __init__(self, workspace: str | Path, run_id: str) -> None:
        if not run_id or run_id in {".", ".."} or "/" in run_id or "\\" in run_id:
            raise RunTransactionError("run_id is not a safe workspace path component")
        self.workspace = Path(workspace)
        self.run_id = run_id
        self.root = self.workspace / self.ROOT_NAME / run_id
        self.manifest_path = self.root / "manifest.json"
        self.run_ledger_path = self.root / "run-decisions.jsonl"
        self.staged_book_path = self.root / "paper_book.next.json"
        self.staged_ledger_path = self.root / "decisions.next.jsonl"
        self.staged_summary_path = self.root / "run-summary.next.json"

    @classmethod
    def start(
        cls,
        workspace: str | Path,
        *,
        run_id: str,
        experiment_key: str,
        market_sha256: str,
        results_sha256: str,
        strategy_id: str,
        base_paper_book_sha256: str,
        base_decision_ledger_sha256: str,
    ) -> "RunTransaction":
        tx = cls(workspace, run_id)
        tx.root.mkdir(parents=True, exist_ok=False)
        manifest = {
            "schema_version": cls.SCHEMA_VERSION,
            "phase": "staging",
            "run_id": run_id,
            "experiment_key": experiment_key,
            "market_sha256": market_sha256,
            "sealed_results_sha256": results_sha256,
            "strategy_id": strategy_id,
            "real_money_execution": False,
            "base": {
                "paper_book_sha256": base_paper_book_sha256,
                "decision_ledger_sha256": base_decision_ledger_sha256,
            },
            "new": {},
            "targets": {
                "paper_book": "paper_book.json",
                "decision_ledger": "decisions.jsonl",
                "summary": f"run-{run_id}.json",
            },
            "staged": {
                "paper_book": "paper_book.next.json",
                "decision_ledger": "decisions.next.jsonl",
                "summary": "run-summary.next.json",
                "run_decisions": "run-decisions.jsonl",
            },
        }
        atomic_write_json(tx.manifest_path, manifest)
        return tx

    def stage_outputs(self, book: PaperBook, canonical_ledger_path: str | Path) -> tuple[str, str]:
        manifest = self._read_manifest()
        if manifest["phase"] != "staging":
            raise RunTransactionError("transaction is not in staging phase")
        canonical_ledger = Path(canonical_ledger_path)
        if canonical_ledger.resolve() != (self.workspace / "decisions.jsonl").resolve():
            raise RunTransactionError("Decision Ledger staging path must be the canonical workspace path")
        ensure_durable_file(canonical_ledger)
        self._require_hash(
            self.workspace / "paper_book.json",
            self._hash_field(manifest, "base", "paper_book_sha256"),
            "PaperBook",
        )
        canonical_snapshot = self._require_decision_ledger_snapshot(
            canonical_ledger,
            self._hash_field(manifest, "base", "decision_ledger_sha256"),
            "Decision Ledger",
        )
        book.save(self.staged_book_path)
        ensure_durable_file(self.run_ledger_path)
        run_snapshot = self._verified_decision_ledger(
            self.run_ledger_path,
            "staged run Decision Ledger",
        )
        self._write_combined_ledger(
            canonical_snapshot.payload,
            run_snapshot.payload,
            self.staged_ledger_path,
        )
        staged_snapshot = self._verified_decision_ledger(
            self.staged_ledger_path,
            "combined staged Decision Ledger",
        )
        return sha256_file(self.staged_book_path), staged_snapshot.sha256

    def precommit(self, summary_payload: dict[str, Any]) -> dict[str, Any]:
        manifest = self._read_manifest()
        if manifest["phase"] != "staging":
            raise RunTransactionError("transaction is not in staging phase")
        if not self.staged_book_path.is_file() or not self.staged_ledger_path.is_file():
            raise RunTransactionError("staged outputs are incomplete")
        self._require_hash(
            self.workspace / "paper_book.json",
            self._hash_field(manifest, "base", "paper_book_sha256"),
            "PaperBook",
        )
        self._require_decision_ledger_snapshot(
            self.workspace / "decisions.jsonl",
            self._hash_field(manifest, "base", "decision_ledger_sha256"),
            "Decision Ledger",
        )
        staged_snapshot = self._verified_decision_ledger(
            self.staged_ledger_path,
            "combined staged Decision Ledger",
        )

        book_hash = sha256_file(self.staged_book_path)
        ledger_hash = staged_snapshot.sha256
        summary = dict(summary_payload)
        summary["paper_book_sha256"] = book_hash
        summary["decision_ledger_sha256"] = ledger_hash
        summary["transaction_schema_version"] = self.SCHEMA_VERSION
        summary["transaction_run_id"] = self.run_id
        atomic_write_json(self.staged_summary_path, summary)
        validated_summary = self._read_strict_json_file(
            self.staged_summary_path,
            label="staged run summary",
        )
        if not isinstance(validated_summary, dict):
            raise RunTransactionError("staged run summary schema is invalid")
        summary_hash = sha256_file(self.staged_summary_path)

        manifest["new"] = {
            "paper_book_sha256": book_hash,
            "decision_ledger_sha256": ledger_hash,
            "summary_sha256": summary_hash,
        }
        manifest["phase"] = "precommitted"
        atomic_write_json(self.manifest_path, manifest)
        return summary

    def commit(self) -> Path:
        manifest = self._read_manifest()
        if manifest["phase"] not in {"precommitted", "canonical_committed", "completed"}:
            raise RunTransactionError("transaction lacks durable precommit evidence")
        self._validate_manifest_paths(manifest)
        # Every artifact that can make the transaction irreversible is preflighted
        # before the first economic os.replace.  In particular, the run summary must
        # remain bound to the same NEW PaperBook and Decision Ledger identities as the
        # manifest; otherwise a tampered manifest could commit mutually inconsistent
        # but individually hash-valid evidence.
        self._validate_precommit_evidence(manifest)
        self._validate_decision_ledger_commit_state(manifest)
        self._promote_base_or_new(
            target=self.workspace / "paper_book.json",
            staged=self.staged_book_path,
            base_hash=self._hash_field(manifest, "base", "paper_book_sha256"),
            new_hash=self._hash_field(manifest, "new", "paper_book_sha256"),
            label="PaperBook",
        )
        self._promote_base_or_new(
            target=self.workspace / "decisions.jsonl",
            staged=self.staged_ledger_path,
            base_hash=self._hash_field(manifest, "base", "decision_ledger_sha256"),
            new_hash=self._hash_field(manifest, "new", "decision_ledger_sha256"),
            label="Decision Ledger",
        )
        summary_target = self.workspace / f"run-{self.run_id}.json"
        self._promote_summary(
            target=summary_target,
            staged=self.staged_summary_path,
            expected_hash=self._hash_field(manifest, "new", "summary_sha256"),
        )
        manifest["phase"] = "canonical_committed"
        atomic_write_json(self.manifest_path, manifest)
        return summary_target

    @classmethod
    def recover(
        cls,
        workspace: str | Path,
        *,
        run_id: str,
        registry_item: dict[str, Any],
        experiment_key: str,
    ) -> TransactionRecovery:
        tx = cls(workspace, run_id)
        if not tx.manifest_path.is_file():
            raise RunTransactionError("transaction manifest not found")
        manifest = tx._read_manifest()
        tx._validate_identity(manifest, registry_item, experiment_key)
        tx._validate_manifest_paths(manifest)

        phase = manifest["phase"]
        if phase == "staging":
            tx._require_hash(
                tx.workspace / "paper_book.json",
                tx._hash_field(manifest, "base", "paper_book_sha256"),
                "PaperBook",
            )
            tx._require_decision_ledger_snapshot(
                tx.workspace / "decisions.jsonl",
                tx._hash_field(manifest, "base", "decision_ledger_sha256"),
                "Decision Ledger",
            )
            summary_target = tx.workspace / f"run-{run_id}.json"
            if summary_target.exists():
                raise RunTransactionError("uncommitted transaction unexpectedly has a canonical run summary")
            manifest["phase"] = "aborted"
            atomic_write_json(tx.manifest_path, manifest)
            return TransactionRecovery("aborted_uncommitted")

        if phase == "aborted":
            tx._require_hash(
                tx.workspace / "paper_book.json",
                tx._hash_field(manifest, "base", "paper_book_sha256"),
                "PaperBook",
            )
            tx._require_decision_ledger_snapshot(
                tx.workspace / "decisions.jsonl",
                tx._hash_field(manifest, "base", "decision_ledger_sha256"),
                "Decision Ledger",
            )
            return TransactionRecovery("aborted_uncommitted")

        if phase in {"precommitted", "canonical_committed", "completed"}:
            summary_path = tx.commit()
            return TransactionRecovery("committed", summary_path)

        raise RunTransactionError(f"unsupported transaction phase: {phase}")

    def mark_registry_completed(self) -> None:
        manifest = self._read_manifest()
        if manifest["phase"] != "canonical_committed":
            raise RunTransactionError("canonical artifacts are not fully committed")
        manifest["phase"] = "completed"
        atomic_write_json(self.manifest_path, manifest)

    @staticmethod
    def _decode_strict_json(text: str, *, label: str) -> Any:
        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            payload: dict[str, Any] = {}
            for key, value in pairs:
                if key in payload:
                    raise RunTransactionError(
                        f"{label} contains duplicate JSON key {key!r}"
                    )
                payload[key] = value
            return payload

        def reject_non_finite(value: str) -> None:
            raise RunTransactionError(
                f"{label} contains non-finite JSON value {value!r}"
            )

        try:
            return json.loads(
                text,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_non_finite,
            )
        except json.JSONDecodeError as exc:
            raise RunTransactionError(f"{label} contains invalid JSON") from exc

    @classmethod
    def _read_strict_json_file(cls, path: Path, *, label: str) -> Any:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RunTransactionError(f"{label} is unreadable or invalid UTF-8") from exc
        return cls._decode_strict_json(text, label=label)

    def _read_manifest(self) -> dict[str, Any]:
        manifest = self._read_strict_json_file(
            self.manifest_path,
            label="transaction manifest",
        )
        schema_version = manifest.get("schema_version") if isinstance(manifest, dict) else None
        if (
            not isinstance(manifest, dict)
            or isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != self.SCHEMA_VERSION
        ):
            raise RunTransactionError("transaction manifest schema is invalid")
        if manifest.get("run_id") != self.run_id:
            raise RunTransactionError("transaction manifest run_id mismatch")
        if manifest.get("real_money_execution") is not False:
            raise RunTransactionError("transaction manifest truth boundary is invalid")
        return manifest

    def _validate_identity(self, manifest: dict[str, Any], item: dict[str, Any], experiment_key: str) -> None:
        expected = {
            "experiment_key": experiment_key,
            "run_id": item.get("run_id"),
            "market_sha256": item.get("market_sha256"),
            "sealed_results_sha256": item.get("results_sha256"),
            "strategy_id": item.get("strategy_id"),
        }
        mismatches = [field for field, expected_value in expected.items() if manifest.get(field) != expected_value]
        if mismatches:
            raise RunTransactionError("transaction identity mismatch: " + ",".join(sorted(mismatches)))

    def _validate_manifest_paths(self, manifest: dict[str, Any]) -> None:
        expected_targets = {
            "paper_book": "paper_book.json",
            "decision_ledger": "decisions.jsonl",
            "summary": f"run-{self.run_id}.json",
        }
        expected_staged = {
            "paper_book": "paper_book.next.json",
            "decision_ledger": "decisions.next.jsonl",
            "summary": "run-summary.next.json",
            "run_decisions": "run-decisions.jsonl",
        }
        if manifest.get("targets") != expected_targets or manifest.get("staged") != expected_staged:
            raise RunTransactionError("transaction manifest paths are invalid")

    @staticmethod
    def _hash_field(manifest: dict[str, Any], section: str, field: str) -> str:
        section_payload = manifest.get(section)
        value = section_payload.get(field) if isinstance(section_payload, dict) else None
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise RunTransactionError(f"transaction manifest lacks {section}.{field} or value is invalid")
        return value

    @staticmethod
    def _require_hash(path: Path, expected_hash: str, label: str) -> None:
        if not path.is_file():
            raise RunTransactionError(f"{label} canonical file is missing")
        if sha256_file(path) != expected_hash:
            raise RunTransactionError(f"{label} SHA-256 canonical hash is not the expected transaction state")

    @staticmethod
    def _verified_decision_ledger(
        path: Path,
        label: str,
    ) -> VerifiedDecisionLedgerSnapshot:
        try:
            return JsonlDecisionLedger(path).verified_snapshot()
        except DecisionLedgerIntegrityError as exc:
            raise RunTransactionError(
                f"{label} integrity validation failed: {exc}"
            ) from exc

    @classmethod
    def _require_decision_ledger_snapshot(
        cls,
        path: Path,
        expected_hash: str,
        label: str,
    ) -> VerifiedDecisionLedgerSnapshot:
        snapshot = cls._verified_decision_ledger(path, label)
        if snapshot.sha256 != expected_hash:
            raise RunTransactionError(
                f"{label} SHA-256 canonical hash is not the expected transaction state"
            )
        return snapshot

    @staticmethod
    def _verify_decision_ledger(path: Path, label: str) -> int:
        """Compatibility wrapper for callers/tests that only need semantic validation."""
        return RunTransaction._verified_decision_ledger(path, label).record_count

    def _validate_precommit_evidence(self, manifest: dict[str, Any]) -> None:
        expected_book_hash = self._hash_field(manifest, "new", "paper_book_sha256")
        expected_ledger_hash = self._hash_field(manifest, "new", "decision_ledger_sha256")
        expected_summary_hash = self._hash_field(manifest, "new", "summary_sha256")
        summary_target = self.workspace / f"run-{self.run_id}.json"

        candidates: list[tuple[str, Path]] = []
        for label, path in (
            ("staged run summary", self.staged_summary_path),
            ("canonical run summary", summary_target),
        ):
            if path.exists():
                if not path.is_file():
                    raise RunTransactionError(f"{label} is not a file")
                candidates.append((label, path))
        if not candidates:
            raise RunTransactionError("run summary precommit artifact is missing")

        for label, path in candidates:
            if sha256_file(path) != expected_summary_hash:
                if label == "canonical run summary":
                    raise RunTransactionError(
                        "canonical run summary SHA-256 mismatch (identity mismatch or SHA-256 mismatch)"
                    )
                raise RunTransactionError(f"{label} SHA-256 mismatch")
            summary = self._read_strict_json_file(path, label=label)
            if not isinstance(summary, dict):
                raise RunTransactionError(f"{label} schema is invalid")
            expected_bindings = {
                "paper_book_sha256": expected_book_hash,
                "decision_ledger_sha256": expected_ledger_hash,
                "transaction_run_id": self.run_id,
            }
            mismatches = [
                field_name
                for field_name, expected_value in expected_bindings.items()
                if summary.get(field_name) != expected_value
            ]
            summary_schema = summary.get("transaction_schema_version")
            if (
                isinstance(summary_schema, bool)
                or not isinstance(summary_schema, int)
                or summary_schema != self.SCHEMA_VERSION
            ):
                mismatches.append("transaction_schema_version")
            if mismatches:
                raise RunTransactionError(
                    f"{label} transaction binding mismatch: " + ",".join(sorted(mismatches))
                )

    def _validate_decision_ledger_commit_state(self, manifest: dict[str, Any]) -> None:
        target = self.workspace / "decisions.jsonl"
        if not target.is_file():
            raise RunTransactionError("Decision Ledger canonical file is missing")
        base_hash = self._hash_field(manifest, "base", "decision_ledger_sha256")
        new_hash = self._hash_field(manifest, "new", "decision_ledger_sha256")
        current_snapshot = self._verified_decision_ledger(
            target,
            "canonical Decision Ledger",
        )
        if current_snapshot.sha256 not in {base_hash, new_hash}:
            raise RunTransactionError(
                "Decision Ledger SHA-256 canonical hash is neither BASE nor NEW"
            )
        if current_snapshot.sha256 == base_hash:
            if not self.staged_ledger_path.is_file():
                raise RunTransactionError("staged Decision Ledger artifact is missing")
            staged_snapshot = self._verified_decision_ledger(
                self.staged_ledger_path,
                "combined staged Decision Ledger",
            )
            if staged_snapshot.sha256 != new_hash:
                raise RunTransactionError("staged Decision Ledger artifact hash mismatch")

    @classmethod
    def _promote_base_or_new(
        cls,
        *,
        target: Path,
        staged: Path,
        base_hash: str,
        new_hash: str,
        label: str,
    ) -> None:
        if not target.is_file():
            raise RunTransactionError(f"{label} canonical file is missing")
        current_hash = sha256_file(target)
        if current_hash == new_hash:
            return
        if current_hash != base_hash:
            raise RunTransactionError(f"{label} SHA-256 canonical hash is neither BASE nor NEW")
        cls._replace_verified(staged, target, new_hash, label)

    @classmethod
    def _promote_summary(cls, *, target: Path, staged: Path, expected_hash: str) -> None:
        if target.exists():
            if not target.is_file() or sha256_file(target) != expected_hash:
                raise RunTransactionError("run summary identity mismatch or SHA-256 mismatch")
            return
        cls._replace_verified(staged, target, expected_hash, "run summary")

    @staticmethod
    def _replace_verified(staged: Path, target: Path, expected_hash: str, label: str) -> None:
        if not staged.is_file():
            raise RunTransactionError(f"staged {label} artifact is missing")
        if sha256_file(staged) != expected_hash:
            raise RunTransactionError(f"staged {label} artifact hash mismatch")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
        if sha256_file(target) != expected_hash:
            raise RunTransactionError(f"committed {label} artifact hash mismatch")

    @staticmethod
    def _write_combined_ledger(base: bytes, appended: bytes, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("wb") as output:
            output.write(base)
            output.write(appended)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
