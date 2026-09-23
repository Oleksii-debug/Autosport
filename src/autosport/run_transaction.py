from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
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


_WINDOWS_MAX_COMPONENT_UTF16_CODE_UNITS = 255
_RUN_SUMMARY_COMPONENT_OVERHEAD_UTF16_CODE_UNITS = len("run-.json".encode("utf-16-le")) // 2
_WINDOWS_MAX_RUN_ID_UTF16_CODE_UNITS = (
    _WINDOWS_MAX_COMPONENT_UTF16_CODE_UNITS - _RUN_SUMMARY_COMPONENT_OVERHEAD_UTF16_CODE_UNITS
)
_WINDOWS_RESERVED_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
        *(f"LPT{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
    }
)


class RunTransactionError(RuntimeError):
    pass


def _require_portable_run_id(run_id: object) -> str:
    """Reject path components that cannot be represented safely by the Windows product."""

    if not isinstance(run_id, str) or not run_id:
        raise RunTransactionError("run_id is not a safe workspace path component")
    try:
        utf16_code_units = len(run_id.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise RunTransactionError("run_id is not a safe workspace path component") from exc
    if utf16_code_units > _WINDOWS_MAX_RUN_ID_UTF16_CODE_UNITS:
        raise RunTransactionError("run_id is not a safe workspace path component")
    if run_id in {".", ".."} or run_id[0] == " " or run_id[-1] in {" ", "."}:
        raise RunTransactionError("run_id is not a safe workspace path component")
    if any(character in _WINDOWS_RESERVED_CHARACTERS or ord(character) < 32 for character in run_id):
        raise RunTransactionError("run_id is not a safe workspace path component")
    device_base = run_id.split(".", 1)[0].upper()
    if device_base in _WINDOWS_RESERVED_DEVICE_NAMES:
        raise RunTransactionError("run_id is not a safe workspace path component")
    return run_id


@dataclass(frozen=True, slots=True)
class TransactionRecovery:
    disposition: str
    summary_path: Path | None = None


@dataclass(frozen=True, slots=True)
class VerifiedFileSnapshot:
    payload: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class TransactionIdentity:
    run_id: str
    experiment_key: str
    market_sha256: str
    sealed_results_sha256: str
    strategy_id: str
    base_paper_book_sha256: str | None
    base_decision_ledger_sha256: str | None


class RunTransaction:
    """Crash-recoverable commit protocol for PaperBook, Decision Ledger and run summary."""

    SCHEMA_VERSION = 1
    ROOT_NAME = ".run-transactions"

    def __init__(self, workspace: str | Path, run_id: str) -> None:
        self.workspace = Path(workspace)
        self.run_id = _require_portable_run_id(run_id)
        self.root = self.workspace / self.ROOT_NAME / self.run_id
        self.manifest_path = self.root / "manifest.json"
        self.base_book_snapshot_path = self.root / "paper_book.base.json"
        self.terminal_book_snapshot_path = self.root / "paper_book.terminal.json"
        self.run_ledger_path = self.root / "run-decisions.jsonl"
        self.staged_book_path = self.root / "paper_book.next.json"
        self.staged_ledger_path = self.root / "decisions.next.jsonl"
        self.staged_summary_path = self.root / "run-summary.next.json"
        self._identity: TransactionIdentity | None = None

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
        base_book_snapshot = tx._verified_canonical_paper_book_snapshot(
            tx.workspace / "paper_book.json",
            "base PaperBook",
        )
        if base_book_snapshot.sha256 != base_paper_book_sha256:
            raise RunTransactionError(
                "base PaperBook SHA-256 canonical hash is not the expected transaction state"
            )

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
        try:
            tx._atomic_write_bytes(
                tx.base_book_snapshot_path,
                base_book_snapshot.payload,
            )
            retained_base = tx._verified_canonical_paper_book_snapshot(
                tx.base_book_snapshot_path,
                "retained base PaperBook",
            )
            if (
                retained_base.sha256 != base_book_snapshot.sha256
                or retained_base.payload != base_book_snapshot.payload
            ):
                raise RunTransactionError(
                    "retained base PaperBook exact snapshot mismatch"
                )
        except BaseException as retention_error:
            # The canonical economic state is still BASE here. Publish an aborted
            # transaction phase before propagating an ordinary retention failure so
            # restart recovery never sees a known-failed start as an ambiguous writer.
            try:
                manifest["phase"] = "aborted"
                atomic_write_json(tx.manifest_path, manifest)
            except BaseException as abort_error:
                retention_error.add_note(
                    "RunTransaction could not persist aborted phase after base "
                    "PaperBook snapshot retention failed: "
                    f"{type(abort_error).__name__}: {abort_error}"
                )
            raise

        tx._identity = TransactionIdentity(
            run_id=run_id,
            experiment_key=experiment_key,
            market_sha256=market_sha256,
            sealed_results_sha256=results_sha256,
            strategy_id=strategy_id,
            base_paper_book_sha256=base_paper_book_sha256,
            base_decision_ledger_sha256=base_decision_ledger_sha256,
        )
        tx._require_complete_identity_anchor()
        return tx

    def verified_base_paper_book_snapshot(self) -> VerifiedFileSnapshot:
        """Return exact BASE bytes after re-resolving the canonical run identity.

        The retained file is evidence for one already-registered run, not a
        standalone authority. Legacy transactions without the retained file or
        transaction-aware registry BASE hashes fail closed only at this accessor.
        """

        manifest = self._read_manifest()
        experiment_key = manifest.get("experiment_key")
        if not isinstance(experiment_key, str) or not experiment_key:
            raise RunTransactionError(
                "retained base PaperBook lacks transaction experiment identity"
            )
        try:
            from .run_registry import RunRegistry

            registry_item = RunRegistry(
                self.workspace / "run_registry.json"
            ).get(experiment_key)
            identity = self._identity_from_registry(
                registry_item,
                experiment_key,
            )
            self._validate_manifest_identity(manifest, identity)
        except RunTransactionError:
            raise
        except Exception as exc:
            raise RunTransactionError(
                "retained base PaperBook cannot validate canonical registry identity"
            ) from exc

        expected_hash = identity.base_paper_book_sha256
        if expected_hash is None:
            raise RunTransactionError(
                "retained base PaperBook lacks transaction-aware registry BASE identity"
            )
        snapshot = self._verified_canonical_paper_book_snapshot(
            self.base_book_snapshot_path,
            "retained base PaperBook",
        )
        if snapshot.sha256 != expected_hash:
            raise RunTransactionError(
                "retained base PaperBook SHA-256 does not match transaction BASE"
            )
        return snapshot

    def verified_terminal_paper_book_snapshot(self) -> VerifiedFileSnapshot:
        """Return exact terminal NEW bytes bound to completed canonical run evidence.

        The terminal sidecar is retained from the already-verified staged NEW
        PaperBook before commit. It is historical evidence only: positive readback
        additionally requires the completed RunRegistry identity and exact terminal
        hashes/summary recorded by this transaction. Later workspace runs may advance
        the canonical PaperBook without changing this per-run snapshot.
        """

        manifest = self._read_manifest()
        if manifest.get("phase") not in {"canonical_committed", "completed"}:
            raise RunTransactionError(
                "retained terminal PaperBook requires a terminal transaction"
            )

        registry_item = self._completed_registry_item()
        self._require_complete_identity_anchor()
        assert self._identity is not None
        self._validate_manifest_identity(manifest, self._identity)
        self._validate_completed_registry_evidence(manifest, registry_item)
        self._validate_precommit_evidence(manifest)

        expected_hash = self._hash_field(
            manifest,
            "new",
            "paper_book_sha256",
        )
        snapshot = self._verified_canonical_paper_book_snapshot(
            self.terminal_book_snapshot_path,
            "retained terminal PaperBook",
        )
        if snapshot.sha256 != expected_hash:
            raise RunTransactionError(
                "retained terminal PaperBook SHA-256 does not match transaction NEW"
            )
        return snapshot

    def stage_outputs(self, book: PaperBook, canonical_ledger_path: str | Path) -> tuple[str, str]:
        self._require_complete_identity_anchor()
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
        try:
            book.save(self.staged_book_path)
        except ValueError as exc:
            raise RunTransactionError(
                f"staged PaperBook semantic validation failed: {exc}"
            ) from exc
        book_snapshot = self._verified_paper_book_snapshot(
            self.staged_book_path,
            "staged PaperBook",
        )
        ensure_durable_file(self.run_ledger_path)
        run_snapshot = self._verified_decision_ledger(
            self.run_ledger_path,
            "staged run Decision Ledger",
        )
        self._require_run_decision_identity(
            run_snapshot,
            expected_run_id=self.run_id,
            label="staged run Decision Ledger",
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
        if staged_snapshot.payload != canonical_snapshot.payload + run_snapshot.payload:
            raise RunTransactionError("combined staged Decision Ledger exact snapshot mismatch")

        # Persist the exact stage boundary before precommit. Later phases must bind
        # to these exact bytes instead of blessing whichever mutable path happens to
        # exist when precommit is called.
        manifest["staged_snapshot"] = {
            "paper_book_sha256": book_snapshot.sha256,
            "decision_ledger_sha256": staged_snapshot.sha256,
            "run_decision_ledger_sha256": run_snapshot.sha256,
        }
        atomic_write_json(self.manifest_path, manifest)
        return book_snapshot.sha256, staged_snapshot.sha256

    def precommit(self, summary_payload: dict[str, Any]) -> dict[str, Any]:
        self._require_complete_identity_anchor()
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
        canonical_snapshot = self._require_decision_ledger_snapshot(
            self.workspace / "decisions.jsonl",
            self._hash_field(manifest, "base", "decision_ledger_sha256"),
            "Decision Ledger",
        )

        expected_book_hash = self._hash_field(
            manifest,
            "staged_snapshot",
            "paper_book_sha256",
        )
        expected_ledger_hash = self._hash_field(
            manifest,
            "staged_snapshot",
            "decision_ledger_sha256",
        )
        expected_run_ledger_hash = self._hash_field(
            manifest,
            "staged_snapshot",
            "run_decision_ledger_sha256",
        )

        book_snapshot = self._verified_paper_book_snapshot(
            self.staged_book_path,
            "staged PaperBook",
        )
        if book_snapshot.sha256 != expected_book_hash:
            raise RunTransactionError("staged PaperBook changed after stage_outputs")

        run_snapshot = self._verified_decision_ledger(
            self.run_ledger_path,
            "staged run Decision Ledger",
        )
        self._require_run_decision_identity(
            run_snapshot,
            expected_run_id=self.run_id,
            label="staged run Decision Ledger",
        )
        if run_snapshot.sha256 != expected_run_ledger_hash:
            raise RunTransactionError("staged run Decision Ledger changed after stage_outputs")

        staged_snapshot = self._verified_decision_ledger(
            self.staged_ledger_path,
            "combined staged Decision Ledger",
        )
        if staged_snapshot.sha256 != expected_ledger_hash:
            raise RunTransactionError("combined staged Decision Ledger changed after stage_outputs")
        if staged_snapshot.payload != canonical_snapshot.payload + run_snapshot.payload:
            raise RunTransactionError("combined staged Decision Ledger exact snapshot mismatch")

        # Retain the exact already-verified NEW bytes before the transaction can
        # become precommitted. Never re-read mutable paper_book.json here: it is
        # still BASE until commit, and a later run may advance it after completion.
        self._atomic_write_bytes(
            self.terminal_book_snapshot_path,
            book_snapshot.payload,
        )
        retained_terminal = self._verified_canonical_paper_book_snapshot(
            self.terminal_book_snapshot_path,
            "retained terminal PaperBook",
        )
        if (
            retained_terminal.sha256 != book_snapshot.sha256
            or retained_terminal.payload != book_snapshot.payload
        ):
            raise RunTransactionError(
                "retained terminal PaperBook exact snapshot mismatch"
            )

        book_hash = book_snapshot.sha256
        ledger_hash = staged_snapshot.sha256
        summary = dict(summary_payload)
        summary["paper_book_sha256"] = book_hash
        summary["decision_ledger_sha256"] = ledger_hash
        summary["transaction_schema_version"] = self.SCHEMA_VERSION
        summary["transaction_run_id"] = self.run_id
        summary_snapshot = self._canonical_json_snapshot(
            summary,
            label="staged run summary",
        )
        decoded_summary = self._decode_file_snapshot_json(
            summary_snapshot,
            label="staged run summary",
        )
        if not isinstance(decoded_summary, dict):
            raise RunTransactionError("staged run summary schema is invalid")
        self._validate_summary_identity(
            decoded_summary,
            manifest,
            label="staged run summary",
        )
        self._atomic_write_bytes(self.staged_summary_path, summary_snapshot.payload)

        manifest["new"] = {
            "paper_book_sha256": book_hash,
            "decision_ledger_sha256": ledger_hash,
            "summary_sha256": summary_snapshot.sha256,
        }
        manifest["phase"] = "precommitted"
        atomic_write_json(self.manifest_path, manifest)
        return summary

    def commit(self) -> Path:
        self._ensure_commit_identity_anchor()
        manifest = self._read_manifest()
        if manifest["phase"] not in {"precommitted", "canonical_committed", "completed"}:
            raise RunTransactionError("transaction lacks durable precommit evidence")
        self._validate_manifest_paths(manifest)
        # Every artifact that can make the transaction irreversible is preflighted
        # before the first economic os.replace. In particular, the run summary must
        # remain bound to the same NEW PaperBook and Decision Ledger identities as the
        # manifest; otherwise a tampered manifest could commit mutually inconsistent
        # but individually hash-valid evidence.
        self._validate_precommit_evidence(manifest)
        self._validate_paper_book_commit_state(manifest)
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
        tx._identity = tx._identity_from_registry(registry_item, experiment_key)
        manifest = tx._read_manifest()
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
            tx._require_complete_identity_anchor()
            summary_path = tx.commit()
            return TransactionRecovery("committed", summary_path)

        raise RunTransactionError(f"unsupported transaction phase: {phase}")

    def mark_registry_completed(self) -> None:
        registry_item = self._completed_registry_item()
        self._require_complete_identity_anchor()
        manifest = self._read_manifest()
        if manifest["phase"] not in {"canonical_committed", "completed"}:
            raise RunTransactionError("canonical artifacts are not fully committed")
        self._validate_manifest_paths(manifest)
        self._validate_completed_registry_evidence(manifest, registry_item)
        expected_book_hash = self._hash_field(manifest, "new", "paper_book_sha256")
        expected_ledger_hash = self._hash_field(manifest, "new", "decision_ledger_sha256")
        book_snapshot = self._verified_canonical_paper_book_snapshot(
            self.workspace / "paper_book.json",
            "PaperBook",
        )
        if book_snapshot.sha256 != expected_book_hash:
            raise RunTransactionError(
                "completed registry PaperBook does not match transaction NEW"
            )
        self._require_decision_ledger_snapshot(
            self.workspace / "decisions.jsonl",
            expected_ledger_hash,
            "Decision Ledger",
        )
        self._validate_precommit_evidence(manifest)
        if manifest["phase"] == "completed":
            return
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
        if self._identity is not None:
            self._validate_manifest_identity(manifest, self._identity)
        return manifest

    def _identity_from_registry(
        self,
        item: dict[str, Any],
        experiment_key: str,
    ) -> TransactionIdentity:
        if not isinstance(item, dict):
            raise RunTransactionError("registry transaction identity is invalid")
        run_id = self._require_identity_text(item.get("run_id"), "run_id")
        if run_id != self.run_id:
            raise RunTransactionError("registry transaction run_id mismatch")
        experiment = self._require_identity_text(experiment_key, "experiment_key")
        market = self._require_identity_hash(item.get("market_sha256"), "market_sha256")
        results = self._require_identity_hash(item.get("results_sha256"), "results_sha256")
        strategy = self._require_identity_text(item.get("strategy_id"), "strategy_id")
        expected_base_identity = hashlib.sha256(
            f"{market}|{results}|{strategy}".encode("utf-8")
        ).hexdigest()
        declared_base_identity = item.get("base_identity")
        if declared_base_identity is not None and declared_base_identity != expected_base_identity:
            raise RunTransactionError("registry base identity is inconsistent")

        base_book_value = item.get("base_paper_book_sha256")
        base_ledger_value = item.get("base_decision_ledger_sha256")
        if (base_book_value is None) != (base_ledger_value is None):
            raise RunTransactionError("registry transaction base hashes are incomplete")
        base_book = (
            None
            if base_book_value is None
            else self._require_identity_hash(base_book_value, "base_paper_book_sha256")
        )
        base_ledger = (
            None
            if base_ledger_value is None
            else self._require_identity_hash(base_ledger_value, "base_decision_ledger_sha256")
        )
        return TransactionIdentity(
            run_id=run_id,
            experiment_key=experiment,
            market_sha256=market,
            sealed_results_sha256=results,
            strategy_id=strategy,
            base_paper_book_sha256=base_book,
            base_decision_ledger_sha256=base_ledger,
        )

    @staticmethod
    def _require_identity_text(value: object, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise RunTransactionError(f"registry transaction {label} is invalid")
        return value

    @staticmethod
    def _require_identity_hash(value: object, label: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise RunTransactionError(f"registry transaction {label} is invalid")
        return value

    def _require_complete_identity_anchor(self) -> None:
        identity = self._identity
        if (
            identity is None
            or identity.base_paper_book_sha256 is None
            or identity.base_decision_ledger_sha256 is None
        ):
            raise RunTransactionError("transaction lacks immutable start identity")

    def _ensure_commit_identity_anchor(self) -> None:
        if self._identity is None:
            self._bind_in_progress_registry_identity()
        self._require_complete_identity_anchor()

    def _bind_in_progress_registry_identity(self) -> None:
        try:
            from .run_registry import RunRegistry

            registry = RunRegistry(self.workspace / "run_registry.json")
            matches = [
                (key, item)
                for key, item in registry.in_progress()
                if item.get("run_id") == self.run_id
            ]
        except Exception as exc:
            raise RunTransactionError(
                "transaction commit cannot validate external registry identity"
            ) from exc
        if len(matches) != 1:
            raise RunTransactionError("transaction commit lacks unique in-progress registry identity")
        experiment_key, item = matches[0]
        self._identity = self._identity_from_registry(item, experiment_key)

    def _completed_registry_item(self) -> dict[str, Any]:
        try:
            from .run_registry import RunRegistry

            registry = RunRegistry(self.workspace / "run_registry.json")
            if self._identity is None:
                manifest = self._read_manifest()
                experiment_key = manifest.get("experiment_key")
                if not isinstance(experiment_key, str) or not experiment_key:
                    raise RunTransactionError(
                        "transaction completion manifest lacks experiment identity"
                    )
            else:
                experiment_key = self._identity.experiment_key
            item = registry.get(experiment_key)
        except RunTransactionError:
            raise
        except Exception as exc:
            raise RunTransactionError(
                "transaction completion cannot validate terminal registry identity"
            ) from exc
        if item.get("status") != "completed":
            raise RunTransactionError(
                "transaction completion requires a completed registry identity"
            )
        candidate_identity = self._identity_from_registry(item, experiment_key)
        if self._identity is not None and candidate_identity != self._identity:
            raise RunTransactionError("transaction completion registry identity mismatch")
        self._identity = candidate_identity
        return item

    def _validate_completed_registry_evidence(
        self,
        manifest: dict[str, Any],
        registry_item: dict[str, Any],
    ) -> None:
        expected = {
            "paper_book_sha256": self._hash_field(manifest, "new", "paper_book_sha256"),
            "decision_ledger_sha256": self._hash_field(
                manifest,
                "new",
                "decision_ledger_sha256",
            ),
        }
        mismatches = [
            field
            for field, expected_value in expected.items()
            if registry_item.get(field) != expected_value
        ]
        result_path = registry_item.get("result_path")
        expected_summary_name = f"run-{self.run_id}.json"
        if (
            not isinstance(result_path, str)
            or not result_path
            or result_path.replace("\\", "/").rsplit("/", 1)[-1] != expected_summary_name
        ):
            mismatches.append("result_path")
        if mismatches:
            raise RunTransactionError(
                "completed registry evidence mismatch: " + ",".join(sorted(mismatches))
            )

    def _validate_manifest_identity(
        self,
        manifest: dict[str, Any],
        identity: TransactionIdentity,
    ) -> None:
        expected = {
            "run_id": identity.run_id,
            "experiment_key": identity.experiment_key,
            "market_sha256": identity.market_sha256,
            "sealed_results_sha256": identity.sealed_results_sha256,
            "strategy_id": identity.strategy_id,
        }
        mismatches = [
            field
            for field, expected_value in expected.items()
            if manifest.get(field) != expected_value
        ]
        base = manifest.get("base")
        if not isinstance(base, dict):
            mismatches.append("base")
        else:
            if (
                identity.base_paper_book_sha256 is not None
                and base.get("paper_book_sha256") != identity.base_paper_book_sha256
            ):
                mismatches.append("base.paper_book_sha256")
            if (
                identity.base_decision_ledger_sha256 is not None
                and base.get("decision_ledger_sha256") != identity.base_decision_ledger_sha256
            ):
                mismatches.append("base.decision_ledger_sha256")
        if mismatches:
            raise RunTransactionError(
                "transaction manifest immutable identity mismatch: "
                + ",".join(sorted(set(mismatches)))
            )

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

    @classmethod
    def _require_hash(cls, path: Path, expected_hash: str, label: str) -> None:
        snapshot = cls._read_canonical_file_snapshot(path, label)
        if snapshot.sha256 != expected_hash:
            raise RunTransactionError(f"{label} SHA-256 canonical hash is not the expected transaction state")

    @staticmethod
    def _read_file_snapshot(path: Path, label: str) -> VerifiedFileSnapshot:
        try:
            with path.open("rb") as handle:
                payload = handle.read()
        except OSError as exc:
            raise RunTransactionError(f"{label} artifact is missing or unreadable") from exc
        return VerifiedFileSnapshot(
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    @staticmethod
    def _read_canonical_file_snapshot(path: Path, label: str) -> VerifiedFileSnapshot:
        """Read exact canonical bytes while rejecting pathname indirection/replacement."""

        def stable_metadata(left: os.stat_result, right: os.stat_result) -> bool:
            return (
                left.st_mode == right.st_mode
                and left.st_size == right.st_size
                and left.st_mtime_ns == right.st_mtime_ns
                and left.st_ctime_ns == right.st_ctime_ns
            )

        def path_matches_open_handle(
            handle: Any,
            expected_path_stat: os.stat_result,
        ) -> bool:
            try:
                current = os.stat(path, follow_symlinks=False)
            except OSError:
                return False
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or not stable_metadata(expected_path_stat, current)
            ):
                return False
            try:
                verification = path.open("rb")
            except OSError:
                return False
            with verification:
                try:
                    same_file = os.path.sameopenfile(
                        handle.fileno(),
                        verification.fileno(),
                    )
                    current_after_open = os.stat(path, follow_symlinks=False)
                except OSError:
                    return False
                return (
                    same_file
                    and stat.S_ISREG(current_after_open.st_mode)
                    and current_after_open.st_nlink == 1
                    and stable_metadata(expected_path_stat, current_after_open)
                )

        try:
            path_before = os.stat(path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise RunTransactionError(f"{label} canonical file is missing") from exc
        except OSError as exc:
            raise RunTransactionError(f"{label} canonical file is unreadable") from exc
        if not stat.S_ISREG(path_before.st_mode):
            raise RunTransactionError(
                f"{label} canonical path must be a regular non-symlink file"
            )
        if path_before.st_nlink != 1:
            raise RunTransactionError(
                f"{label} canonical path must not have hard-link aliases"
            )

        try:
            handle = path.open("rb")
        except FileNotFoundError as exc:
            raise RunTransactionError(
                f"{label} canonical path changed while validating"
            ) from exc
        except OSError as exc:
            raise RunTransactionError(f"{label} canonical file is unreadable") from exc

        with handle:
            try:
                opened_before = os.fstat(handle.fileno())
            except OSError as exc:
                raise RunTransactionError(
                    f"{label} canonical path changed while validating"
                ) from exc
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or opened_before.st_nlink != 1
                or not path_matches_open_handle(handle, path_before)
            ):
                raise RunTransactionError(
                    f"{label} canonical path must be a stable regular non-symlink file"
                )

            try:
                payload = handle.read()
                opened_after = os.fstat(handle.fileno())
            except OSError as exc:
                raise RunTransactionError(
                    f"{label} canonical path changed while validating"
                ) from exc
            if (
                not stat.S_ISREG(opened_after.st_mode)
                or opened_after.st_nlink != 1
                or not stable_metadata(opened_before, opened_after)
                or not path_matches_open_handle(handle, path_before)
            ):
                raise RunTransactionError(
                    f"{label} canonical path changed while validating"
                )

        return VerifiedFileSnapshot(
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    @classmethod
    def _canonical_json_snapshot(
        cls,
        payload: dict[str, Any],
        *,
        label: str,
    ) -> VerifiedFileSnapshot:
        try:
            text = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
        except (TypeError, ValueError) as exc:
            raise RunTransactionError(f"{label} cannot be serialized as JSON") from exc
        # Strict re-decode rejects NaN/Infinity and duplicate ambiguity on the exact
        # canonical bytes before their digest can become durable transaction evidence.
        cls._decode_strict_json(text, label=label)
        encoded = text.encode("utf-8")
        return VerifiedFileSnapshot(
            payload=encoded,
            sha256=hashlib.sha256(encoded).hexdigest(),
        )

    @classmethod
    def _decode_file_snapshot_json(
        cls,
        snapshot: VerifiedFileSnapshot,
        *,
        label: str,
    ) -> Any:
        try:
            text = snapshot.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RunTransactionError(f"{label} is invalid UTF-8") from exc
        return cls._decode_strict_json(text, label=label)

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    @classmethod
    def _validate_paper_book_snapshot(
        cls,
        snapshot: VerifiedFileSnapshot,
        path: Path,
        label: str,
    ) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=path.parent,
                prefix=f".{path.name}.verify-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(snapshot.payload)
                handle.flush()
                os.fsync(handle.fileno())
            verification_copy = cls._read_file_snapshot(
                temporary,
                f"{label} verification copy",
            )
            if verification_copy.sha256 != snapshot.sha256:
                raise RunTransactionError(f"{label} exact snapshot copy mismatch")
            PaperBook.load(temporary)
            verification_after = cls._read_file_snapshot(
                temporary,
                f"{label} verification copy",
            )
            if verification_after.sha256 != snapshot.sha256:
                raise RunTransactionError(f"{label} changed during semantic validation")
        except RunTransactionError:
            raise
        except Exception as exc:
            raise RunTransactionError(f"{label} semantic validation failed: {exc}") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    @classmethod
    def _verified_paper_book_snapshot(
        cls,
        path: Path,
        label: str,
    ) -> VerifiedFileSnapshot:
        snapshot = cls._read_file_snapshot(path, label)
        cls._validate_paper_book_snapshot(snapshot, path, label)
        return snapshot

    @classmethod
    def _verified_canonical_paper_book_snapshot(
        cls,
        path: Path,
        label: str,
    ) -> VerifiedFileSnapshot:
        snapshot = cls._read_canonical_file_snapshot(path, label)
        cls._validate_paper_book_snapshot(snapshot, path, f"canonical {label}")
        return snapshot

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
    def _verified_canonical_decision_ledger(
        cls,
        path: Path,
        label: str,
    ) -> VerifiedDecisionLedgerSnapshot:
        snapshot = cls._read_canonical_file_snapshot(path, label)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=path.parent,
                prefix=f".{path.name}.verify-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(snapshot.payload)
                handle.flush()
                os.fsync(handle.fileno())
            verified = cls._verified_decision_ledger(
                temporary,
                f"canonical {label} verification copy",
            )
            if verified.sha256 != snapshot.sha256 or verified.payload != snapshot.payload:
                raise RunTransactionError(
                    f"canonical {label} exact snapshot copy mismatch"
                )
            return verified
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    @classmethod
    def _require_run_decision_identity(
        cls,
        snapshot: VerifiedDecisionLedgerSnapshot,
        *,
        expected_run_id: str,
        label: str,
    ) -> None:
        """Bind every run-local decision in an immutable verified snapshot to this transaction."""

        if snapshot.record_count == 0:
            return
        for line_number, line in enumerate(snapshot.payload.decode("utf-8").splitlines(), start=1):
            envelope = cls._decode_strict_json(line, label=f"{label} line {line_number}")
            record = envelope.get("record") if isinstance(envelope, dict) else None
            actual_run_id = record.get("replay_run_id") if isinstance(record, dict) else None
            if actual_run_id != expected_run_id:
                raise RunTransactionError(
                    f"{label} replay_run_id mismatch at line {line_number}: "
                    f"expected {expected_run_id!r}, got {actual_run_id!r}"
                )

    def _validate_summary_identity(
        self,
        summary: dict[str, Any],
        manifest: dict[str, Any],
        *,
        label: str,
    ) -> None:
        """Bind run-summary truth to the durable transaction identity."""

        expected = {
            "run_id": self.run_id,
            "experiment_key": manifest.get("experiment_key"),
            "market_sha256": manifest.get("market_sha256"),
            "sealed_results_sha256": manifest.get("sealed_results_sha256"),
            "strategy_id": manifest.get("strategy_id"),
        }
        mismatches = [
            field_name
            for field_name, expected_value in expected.items()
            if summary.get(field_name) != expected_value
        ]
        if summary.get("real_money_execution") is not False:
            mismatches.append("real_money_execution")
        if mismatches:
            raise RunTransactionError(
                f"{label} transaction identity mismatch: " + ",".join(sorted(mismatches))
            )

    @classmethod
    def _require_decision_ledger_snapshot(
        cls,
        path: Path,
        expected_hash: str,
        label: str,
    ) -> VerifiedDecisionLedgerSnapshot:
        snapshot = cls._verified_canonical_decision_ledger(path, label)
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

        terminal_snapshot = self._verified_canonical_paper_book_snapshot(
            self.terminal_book_snapshot_path,
            "retained terminal PaperBook",
        )
        if terminal_snapshot.sha256 != expected_book_hash:
            raise RunTransactionError(
                "retained terminal PaperBook SHA-256 does not match transaction NEW"
            )

        summary_target = self.workspace / f"run-{self.run_id}.json"

        if summary_target.exists():
            candidates = [("canonical run summary", summary_target, True)]
        elif self.staged_summary_path.exists():
            if not self.staged_summary_path.is_file():
                raise RunTransactionError("staged run summary is not a file")
            candidates = [("staged run summary", self.staged_summary_path, False)]
        else:
            raise RunTransactionError("run summary precommit artifact is missing")

        for label, path, canonical in candidates:
            snapshot = (
                self._read_canonical_file_snapshot(path, label)
                if canonical
                else self._read_file_snapshot(path, label)
            )
            if snapshot.sha256 != expected_summary_hash:
                if label == "canonical run summary":
                    raise RunTransactionError(
                        "canonical run summary SHA-256 mismatch (identity mismatch or SHA-256 mismatch)"
                    )
                raise RunTransactionError(f"{label} SHA-256 mismatch")
            summary = self._decode_file_snapshot_json(snapshot, label=label)
            if not isinstance(summary, dict):
                raise RunTransactionError(f"{label} schema is invalid")
            self._validate_summary_identity(summary, manifest, label=label)
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

    def _validate_paper_book_commit_state(self, manifest: dict[str, Any]) -> None:
        target = self.workspace / "paper_book.json"
        base_hash = self._hash_field(manifest, "base", "paper_book_sha256")
        new_hash = self._hash_field(manifest, "new", "paper_book_sha256")
        current_snapshot = self._verified_canonical_paper_book_snapshot(
            target,
            "PaperBook",
        )
        if current_snapshot.sha256 not in {base_hash, new_hash}:
            raise RunTransactionError("PaperBook SHA-256 canonical hash is neither BASE nor NEW")
        if current_snapshot.sha256 == base_hash:
            if not self.staged_book_path.is_file():
                raise RunTransactionError("staged PaperBook artifact is missing")
            staged_snapshot = self._read_file_snapshot(
                self.staged_book_path,
                "staged PaperBook",
            )
            if staged_snapshot.sha256 != new_hash:
                raise RunTransactionError("staged PaperBook artifact hash mismatch")
            self._validate_paper_book_snapshot(
                staged_snapshot,
                self.staged_book_path,
                "staged PaperBook",
            )

    def _validate_decision_ledger_commit_state(self, manifest: dict[str, Any]) -> None:
        target = self.workspace / "decisions.jsonl"
        base_hash = self._hash_field(manifest, "base", "decision_ledger_sha256")
        new_hash = self._hash_field(manifest, "new", "decision_ledger_sha256")
        current_snapshot = self._verified_canonical_decision_ledger(
            target,
            "Decision Ledger",
        )
        if current_snapshot.sha256 not in {base_hash, new_hash}:
            raise RunTransactionError(
                "Decision Ledger SHA-256 canonical hash is neither BASE nor NEW"
            )
        if current_snapshot.sha256 == base_hash:
            if not self.staged_ledger_path.is_file():
                raise RunTransactionError("staged Decision Ledger artifact is missing")
            run_snapshot = self._verified_decision_ledger(
                self.run_ledger_path,
                "staged run Decision Ledger",
            )
            self._require_run_decision_identity(
                run_snapshot,
                expected_run_id=self.run_id,
                label="staged run Decision Ledger",
            )
            staged_snapshot = self._verified_decision_ledger(
                self.staged_ledger_path,
                "combined staged Decision Ledger",
            )
            if staged_snapshot.sha256 != new_hash:
                raise RunTransactionError("staged Decision Ledger artifact hash mismatch")
            if staged_snapshot.payload != current_snapshot.payload + run_snapshot.payload:
                raise RunTransactionError("combined staged Decision Ledger exact snapshot mismatch")

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
        current_snapshot = cls._read_canonical_file_snapshot(target, label)
        if current_snapshot.sha256 == new_hash:
            return
        if current_snapshot.sha256 != base_hash:
            raise RunTransactionError(f"{label} SHA-256 canonical hash is neither BASE nor NEW")
        cls._replace_verified(staged, target, new_hash, label)

    @classmethod
    def _promote_summary(cls, *, target: Path, staged: Path, expected_hash: str) -> None:
        if target.exists():
            current_snapshot = cls._read_canonical_file_snapshot(target, "run summary")
            if current_snapshot.sha256 != expected_hash:
                raise RunTransactionError("run summary identity mismatch or SHA-256 mismatch")
            return
        cls._replace_verified(staged, target, expected_hash, "run summary")

    @classmethod
    def _replace_verified(cls, staged: Path, target: Path, expected_hash: str, label: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            digest = hashlib.sha256()
            try:
                source = staged.open("rb")
            except OSError as exc:
                raise RunTransactionError(f"staged {label} artifact is missing or unreadable") from exc
            with source:
                with tempfile.NamedTemporaryFile(
                    "wb",
                    dir=target.parent,
                    prefix=f".{target.name}.promote-",
                    suffix=".tmp",
                    delete=False,
                ) as output:
                    temporary = Path(output.name)
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if digest.hexdigest() != expected_hash:
                raise RunTransactionError(f"staged {label} artifact hash mismatch")
            os.replace(temporary, target)
            temporary = None
            committed_snapshot = cls._read_canonical_file_snapshot(target, label)
            if committed_snapshot.sha256 != expected_hash:
                raise RunTransactionError(f"committed {label} artifact hash mismatch")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

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