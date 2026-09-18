from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path

from .integrity import atomic_write_json, sha256_file
from .workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockBusyError,
    _open_read_only_descriptor,
    _stable_stat_metadata,
)
from .outcome_trust import (
    OutcomeLineageBinding,
    OutcomeLineageTrustError,
    assert_compatible_outcome_lineages,
    outcome_lineage_binding_from_payload,
    outcome_lineage_payload,
)


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
        "outcome_lineage",
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
_FIRST_OPEN_RETRY_SECONDS = 0.01
_FIRST_OPEN_MAX_WAIT_SECONDS = 5.0
_LEGACY_SCHEMA_VERSION = 1
_LINEAGE_TRUST_SCHEMA_VERSION = 2
_LINEAGE_TRUST_FIELD = "outcome_lineage_trust"


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


def _portable_basename(value: str) -> str:
    """Return a durable path basename independent of POSIX/Windows separators."""
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _path_matches_open_descriptor(
    path: Path,
    descriptor: int,
    expected_path_stat: os.stat_result,
) -> bool:
    """Prove the current no-follow pathname still names the already-open descriptor."""

    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or not _stable_stat_metadata(expected_path_stat, current)
    ):
        return False

    try:
        verification_descriptor = _open_read_only_descriptor(path)
    except OSError:
        return False
    matched = False
    try:
        try:
            same_file = os.path.sameopenfile(descriptor, verification_descriptor)
            current_after_open = os.stat(path, follow_symlinks=False)
        except OSError:
            return False
        matched = (
            same_file
            and stat.S_ISREG(current_after_open.st_mode)
            and current_after_open.st_nlink == 1
            and _stable_stat_metadata(expected_path_stat, current_after_open)
        )
    finally:
        try:
            os.close(verification_descriptor)
        except OSError:
            return False
    return matched


def has_durable_workspace_history(workspace: str | Path) -> bool:
    """Return whether a missing registry would discard surviving economic/run evidence.

    A pristine readable zero-byte Decision Ledger and an empty transaction directory are
    allowed first-open artifacts. Everything else named here is durable product history
    or filesystem uncertainty and must make missing-registry initialization fail closed.
    """

    root = Path(workspace)
    transaction_root = root / ".run-transactions"
    transaction_stat = _lstat_or_none(transaction_root)
    if transaction_stat is not None:
        if not stat.S_ISDIR(transaction_stat.st_mode):
            return True
        try:
            next(transaction_root.iterdir())
        except StopIteration:
            pass
        else:
            return True

    for entry in root.iterdir():
        if entry.name.startswith("run-") and entry.name.endswith(".json"):
            return True

    if _lstat_or_none(root / "paper_book.json") is not None:
        return True

    ledger_path = root / "decisions.jsonl"
    ledger_stat = _lstat_or_none(ledger_path)
    if ledger_stat is not None:
        if (
            not stat.S_ISREG(ledger_stat.st_mode)
            or ledger_stat.st_nlink != 1
            or ledger_stat.st_size > 0
        ):
            return True
        try:
            descriptor = _open_read_only_descriptor(ledger_path)
        except OSError:
            return True

        ledger_is_pristine = False
        try:
            try:
                opened_before = os.fstat(descriptor)
            except OSError:
                return True
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or opened_before.st_nlink != 1
                or not _path_matches_open_descriptor(ledger_path, descriptor, ledger_stat)
            ):
                return True

            try:
                first_byte = os.read(descriptor, 1)
                opened_after = os.fstat(descriptor)
            except OSError:
                return True
            if (
                first_byte
                or not stat.S_ISREG(opened_after.st_mode)
                or opened_after.st_nlink != 1
                or not _stable_stat_metadata(opened_before, opened_after)
                or not _path_matches_open_descriptor(ledger_path, descriptor, ledger_stat)
            ):
                return True
            ledger_is_pristine = True
        finally:
            try:
                os.close(descriptor)
            except OSError:
                ledger_is_pristine = False
        if not ledger_is_pristine:
            return True
    return False


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
        try:
            self._read_existing()
        except FileNotFoundError as exc:
            raise ValueError("run registry is missing") from exc

    @classmethod
    def initialize_pristine(cls, path: str | Path) -> "RunRegistry":
        """Explicitly create the first registry only for a verified pristine workspace.

        Ordinary construction is a read/verification operation and never publishes missing
        durable state. Product startup is the sole first-open creation boundary and uses
        this method, which serializes publication with the canonical workspace economic lock.
        """

        registry = cls.__new__(cls)
        registry.path = Path(path)
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            registry._read_existing()
        except FileNotFoundError:
            registry._initialize_missing_registry()
        return registry

    def _read_existing_bytes(self) -> bytes:
        """Read bytes only from the exact stable regular object named by the registry path."""

        path_before = _lstat_or_none(self.path)
        if path_before is None:
            raise FileNotFoundError(self.path)
        if not stat.S_ISREG(path_before.st_mode) or path_before.st_nlink != 1:
            raise ValueError("run registry path is not a regular non-aliased file")

        try:
            descriptor = _open_read_only_descriptor(self.path)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError("run registry path is unreadable") from exc

        primary_error: BaseException | None = None
        try:
            try:
                opened_before = os.fstat(descriptor)
            except OSError as exc:
                raise ValueError("run registry path changed while validating") from exc
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or opened_before.st_nlink != 1
                or not _path_matches_open_descriptor(self.path, descriptor, path_before)
            ):
                raise ValueError("run registry path changed while validating")

            chunks: list[bytes] = []
            try:
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                opened_after = os.fstat(descriptor)
            except OSError as exc:
                raise ValueError("run registry path changed or became unreadable while validating") from exc

            if (
                not stat.S_ISREG(opened_after.st_mode)
                or opened_after.st_nlink != 1
                or not _stable_stat_metadata(opened_before, opened_after)
                or not _path_matches_open_descriptor(self.path, descriptor, path_before)
            ):
                raise ValueError("run registry path changed while validating")
            return b"".join(chunks)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                os.close(descriptor)
            except OSError as close_error:
                if primary_error is None:
                    raise ValueError("run registry descriptor cleanup failed") from close_error
                try:
                    primary_error.add_note(
                        "run registry descriptor cleanup also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                except BaseException:
                    pass

    def _read_existing(self) -> dict:
        """Read and validate only bytes bound to the current canonical registry object."""

        return self._parse_registry_payload(self._read_existing_bytes())

    def _initialize_missing_registry(self) -> None:
        """Serialize first publication against every cooperating economic writer.

        The initial missing-path observation is never publication authority. We first
        acquire the canonical workspace lock, then re-read the registry under that lock.
        If another process already owns the lock, a registry it has durably published can
        be adopted immediately; otherwise we briefly retry until that writer publishes or
        releases. The bounded retry also guarantees a caller that already owns the lock
        cannot deadlock itself if an external actor removed the registry unexpectedly.
        """

        deadline = time.monotonic() + _FIRST_OPEN_MAX_WAIT_SECONDS
        while True:
            lock = WorkspaceEconomicLock(self.path.parent)
            try:
                lock.acquire()
            except WorkspaceEconomicLockBusyError as contention:
                # Only typed native advisory-lock contention permits winner re-read.
                # Alias, identity, creation and backend failures remain fail-closed and
                # propagate without being reclassified as another writer's ownership.
                try:
                    self._read_existing()
                except FileNotFoundError:
                    if time.monotonic() >= deadline:
                        raise WorkspaceEconomicLockBusyError(
                            "run registry first-open could not serialize with the active economic writer"
                        ) from contention
                    time.sleep(_FIRST_OPEN_RETRY_SECONDS)
                    continue
                return

            primary_error: BaseException | None = None
            try:
                # The winner may have published while this process was waiting for
                # the OS lock. Existing bytes are authoritative and are never replaced
                # with a stale empty state.
                try:
                    self._read_existing()
                except FileNotFoundError:
                    pass
                else:
                    return
                try:
                    durable_history = has_durable_workspace_history(self.path.parent)
                except OSError as exc:
                    raise ValueError(
                        "cannot determine durable workspace history while run registry is missing"
                    ) from exc
                if durable_history:
                    raise ValueError("run registry is missing while durable run history exists")
                self._write({"schema_version": 1, "runs": {}})
                # Verify the exact published registry before exposing this object.
                self._read_existing()
                return
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if primary_error is None:
                    lock.release()
                else:
                    try:
                        lock.release()
                    except BaseException as release_error:
                        try:
                            primary_error.add_note(
                                "WorkspaceEconomicLock release also failed during run registry first-open: "
                                f"{type(release_error).__name__}: {release_error}"
                            )
                        except BaseException:
                            pass

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

    def assert_outcome_lineage_compatible(self, binding: OutcomeLineageBinding) -> None:
        """Reject a restart/fork before any new economic base is materialized."""
        if not isinstance(binding, OutcomeLineageBinding):
            raise ValueError("outcome lineage binding must be an OutcomeLineageBinding")
        self._assert_outcome_lineage_compatible_state(self._read(), binding)

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
        outcome_lineage: OutcomeLineageBinding | None = None,
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
        if outcome_lineage is not None and not isinstance(outcome_lineage, OutcomeLineageBinding):
            raise ValueError("outcome_lineage must be an OutcomeLineageBinding or null")

        state = self._read()
        if outcome_lineage is not None:
            self._assert_outcome_lineage_compatible_state(state, outcome_lineage)
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
        if any(item["run_id"] == run_id for item in state["runs"].values()):
            raise RepeatedExperimentError(
                "This run_id already has durable history in this workspace."
            )

        if not existing_pairs:
            key = base_identity
        elif completed:
            key = f"{base_identity}:repeat:{run_id}"
        else:
            key = f"{base_identity}:retry:{run_id}"
        if key in state["runs"]:
            raise RepeatedExperimentError(
                "This run_id already has durable history for this dataset/strategy."
            )

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
        if outcome_lineage is not None:
            self._record_outcome_lineage_trust_state(state, outcome_lineage)
            entry["outcome_lineage"] = outcome_lineage_payload(outcome_lineage)
        state["runs"][key] = entry
        self._validate_entry(key, entry)
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

    def _durable_summary_lineage_bindings(self) -> tuple[OutcomeLineageBinding, ...]:
        """Recover lineage trust duplicated into checksum-bound completed summaries.

        Legacy summaries carry no such field and remain outside this check. A summary
        that does carry the field must still be bound to its existing transaction
        manifest SHA-256 so registry downgrade detection cannot trust an unbound copy.
        """
        bindings: list[OutcomeLineageBinding] = []
        for summary_path in sorted(self.path.parent.glob("run-*.json")):
            if not summary_path.is_file():
                continue
            try:
                summary_bytes = summary_path.read_bytes()
                text = summary_bytes.decode("utf-8")
            except (OSError, UnicodeError):
                continue
            try:
                summary = json.loads(
                    summary_bytes,
                    object_pairs_hook=_reject_duplicate_json_keys,
                    parse_constant=_reject_nonfinite_json_constant,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError("durable lineage-trust run summary is invalid") from exc
            if not isinstance(summary, dict):
                raise ValueError("durable lineage-trust run summary is invalid")
            raw_binding = summary.get(_LINEAGE_TRUST_FIELD)
            run_id = summary.get("run_id")
            if not isinstance(run_id, str) or not run_id or summary_path.name != f"run-{run_id}.json":
                if raw_binding is None:
                    continue
                raise ValueError("durable lineage-trust run summary identity is invalid")

            manifest_path = self.path.parent / ".run-transactions" / run_id / "manifest.json"
            if not manifest_path.is_file():
                if raw_binding is None:
                    continue
                raise ValueError("durable lineage-trust run summary lacks transaction manifest")
            try:
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8"),
                    object_pairs_hook=_reject_duplicate_json_keys,
                    parse_constant=_reject_nonfinite_json_constant,
                )
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError("durable lineage-trust transaction manifest is invalid") from exc
            if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
                raise ValueError("durable lineage-trust transaction manifest identity is invalid")
            if raw_binding is None and manifest.get("phase") not in {"canonical_committed", "completed"}:
                continue
            targets = manifest.get("targets")
            if not isinstance(targets, dict) or targets.get("summary") != summary_path.name:
                raise ValueError("durable lineage-trust transaction summary target is invalid")
            new_state = manifest.get("new")
            expected_summary_sha = (
                new_state.get("summary_sha256") if isinstance(new_state, dict) else None
            )
            if not _is_canonical_sha256(expected_summary_sha):
                raise ValueError("durable lineage-trust transaction lacks summary SHA-256")
            if hashlib.sha256(summary_bytes).hexdigest() != expected_summary_sha:
                raise ValueError("durable lineage-trust run summary SHA-256 mismatch")
            if raw_binding is None:
                continue
            try:
                binding = outcome_lineage_binding_from_payload(
                    raw_binding,
                    context="durable run summary outcome_lineage_trust",
                )
            except OutcomeLineageTrustError as exc:
                raise ValueError("durable run summary contains invalid outcome lineage trust") from exc
            bindings.append(binding)
        return tuple(bindings)

    @staticmethod
    def _outcome_lineage_trust_bindings(
        state: dict,
    ) -> dict[tuple[str, str], OutcomeLineageBinding]:
        if state.get("schema_version") != _LINEAGE_TRUST_SCHEMA_VERSION:
            return {}
        raw_trust = state.get(_LINEAGE_TRUST_FIELD)
        if not isinstance(raw_trust, list) or not raw_trust:
            raise ValueError("run registry lineage-trust schema requires durable trust bindings")
        bindings: dict[tuple[str, str], OutcomeLineageBinding] = {}
        for raw_binding in raw_trust:
            try:
                binding = outcome_lineage_binding_from_payload(
                    raw_binding,
                    context="run registry outcome_lineage_trust",
                )
            except OutcomeLineageTrustError as exc:
                raise ValueError("run registry contains invalid outcome lineage trust") from exc
            identity = (binding.source_identity, binding.record_id)
            if identity in bindings:
                raise ValueError("run registry contains duplicate outcome lineage trust identity")
            bindings[identity] = binding
        return bindings

    @classmethod
    def _record_outcome_lineage_trust_state(
        cls,
        state: dict,
        incoming: OutcomeLineageBinding,
    ) -> None:
        if state.get("schema_version") == _LEGACY_SCHEMA_VERSION:
            if any("outcome_lineage" in item for item in state["runs"].values()):
                raise ValueError(
                    "legacy run registry cannot migrate outcome lineage evidence without durable trust binding"
                )
            state["schema_version"] = _LINEAGE_TRUST_SCHEMA_VERSION
            state[_LINEAGE_TRUST_FIELD] = []
            bindings: dict[tuple[str, str], OutcomeLineageBinding] = {}
        else:
            bindings = cls._outcome_lineage_trust_bindings(state)

        identity = (incoming.source_identity, incoming.record_id)
        trusted = bindings.get(identity)
        if trusted is not None:
            assert_compatible_outcome_lineages(trusted, incoming)
            if len(incoming.revisions) <= len(trusted.revisions):
                return

        payload = outcome_lineage_payload(incoming)
        raw_trust = state[_LINEAGE_TRUST_FIELD]
        if trusted is None:
            raw_trust.append(payload)
        else:
            for index, raw_binding in enumerate(raw_trust):
                candidate = outcome_lineage_binding_from_payload(
                    raw_binding,
                    context="run registry outcome_lineage_trust",
                )
                if (candidate.source_identity, candidate.record_id) == identity:
                    raw_trust[index] = payload
                    break
            else:
                raise ValueError("run registry lost an accepted outcome lineage trust binding")
        raw_trust.sort(key=lambda value: (value["source_identity"], value["record_id"]))

    @classmethod
    def _assert_outcome_lineage_compatible_state(
        cls,
        state: dict,
        incoming: OutcomeLineageBinding,
    ) -> None:
        trusted_bindings = cls._outcome_lineage_trust_bindings(state)
        trusted = trusted_bindings.get((incoming.source_identity, incoming.record_id))
        if trusted is not None:
            assert_compatible_outcome_lineages(trusted, incoming)
        for item in state["runs"].values():
            raw_lineage = item.get("outcome_lineage")
            if raw_lineage is None:
                continue
            lineage = outcome_lineage_binding_from_payload(
                raw_lineage,
                context="run registry outcome_lineage",
            )
            assert_compatible_outcome_lineages(lineage, incoming)

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

        if "outcome_lineage" in item:
            try:
                outcome_lineage_binding_from_payload(
                    item["outcome_lineage"],
                    context="run registry outcome_lineage",
                )
            except OutcomeLineageTrustError as exc:
                raise ValueError("run registry contains invalid outcome lineage evidence") from exc
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
            if base_book_present:
                required_transaction_terminal_fields = {
                    "result_path",
                    "paper_book_sha256",
                    "decision_ledger_sha256",
                }
                if not required_transaction_terminal_fields.issubset(fields):
                    raise ValueError("completed transaction-aware run registry entry lacks terminal economic evidence")
                result_path = item.get("result_path")
                if not isinstance(result_path, str) or not result_path:
                    raise ValueError("completed transaction-aware run registry entry lacks result_path evidence")
                if _portable_basename(result_path) != f"run-{run_id}.json":
                    raise ValueError(
                        "completed transaction-aware run registry result_path does not match run_id"
                    )
            if item.get("reconciled_from_summary") is True:
                result_path = item.get("result_path")
                if not isinstance(result_path, str) or not result_path:
                    raise ValueError("reconciled run registry entry lacks result_path evidence")
                if _portable_basename(result_path) != f"run-{run_id}.json":
                    raise ValueError("reconciled run registry result_path does not match run_id")
                if "paper_book_sha256" not in fields:
                    raise ValueError("reconciled run registry entry lacks PaperBook hash evidence")

    def _parse_registry_payload(self, payload: bytes) -> dict:
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid run registry") from exc

        schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
        if not isinstance(raw, dict) or isinstance(schema_version, bool) or not isinstance(
            schema_version, int
        ):
            raise ValueError("invalid run registry")
        if schema_version == _LEGACY_SCHEMA_VERSION:
            if set(raw) != {"schema_version", "runs"}:
                raise ValueError("invalid run registry")
        elif schema_version == _LINEAGE_TRUST_SCHEMA_VERSION:
            if set(raw) != {"schema_version", "runs", _LINEAGE_TRUST_FIELD}:
                raise ValueError("invalid run registry")
        else:
            raise ValueError("invalid run registry")
        if not isinstance(raw.get("runs"), dict):
            raise ValueError("invalid run registry")

        # Scan transaction-bound summaries when validating a trust-bearing registry,
        # or when a downgraded/empty registry has no run entries left to validate.
        # Active legacy transactions are reconciled by the recovery path itself.
        durable_summary_bindings = (
            self._durable_summary_lineage_bindings()
            if schema_version == _LINEAGE_TRUST_SCHEMA_VERSION or not raw["runs"]
            else ()
        )
        if schema_version == _LEGACY_SCHEMA_VERSION and durable_summary_bindings:
            raise ValueError(
                "run registry lineage-trust schema was downgraded despite durable run summary evidence"
            )

        trusted_bindings = self._outcome_lineage_trust_bindings(raw)
        if schema_version == _LINEAGE_TRUST_SCHEMA_VERSION:
            for durable in durable_summary_bindings:
                identity = (durable.source_identity, durable.record_id)
                trusted = trusted_bindings.get(identity)
                if trusted is None:
                    raise ValueError(
                        "run registry lost lineage trust preserved by durable run summary"
                    )
                try:
                    assert_compatible_outcome_lineages(trusted, durable)
                except OutcomeLineageTrustError as exc:
                    raise ValueError(
                        "run registry conflicts with lineage trust preserved by durable run summary"
                    ) from exc
                if len(trusted.revisions) < len(durable.revisions):
                    raise ValueError(
                        "run registry lineage trust is older than durable run summary evidence"
                    )

        seen_run_ids: set[str] = set()
        longest_lineage_by_identity: dict[tuple[str, str], OutcomeLineageBinding] = {}
        for key, item in raw["runs"].items():
            self._validate_entry(key, item)
            run_id = item["run_id"]
            if run_id in seen_run_ids:
                raise ValueError("run registry contains duplicate run_id evidence")
            seen_run_ids.add(run_id)
            raw_lineage = item.get("outcome_lineage")
            if raw_lineage is None:
                continue
            if schema_version != _LINEAGE_TRUST_SCHEMA_VERSION:
                raise ValueError("legacy run registry cannot contain outcome lineage evidence")
            try:
                lineage = outcome_lineage_binding_from_payload(
                    raw_lineage,
                    context="run registry outcome_lineage",
                )
                identity = (lineage.source_identity, lineage.record_id)
                durable_trust = trusted_bindings.get(identity)
                if durable_trust is None:
                    raise ValueError(
                        "run registry outcome lineage lacks registry-level trust binding"
                    )
                assert_compatible_outcome_lineages(durable_trust, lineage)
                if len(lineage.revisions) > len(durable_trust.revisions):
                    raise ValueError(
                        "run registry outcome lineage exceeds registry-level trust history"
                    )
                trusted = longest_lineage_by_identity.get(identity)
                if trusted is not None:
                    assert_compatible_outcome_lineages(trusted, lineage)
                if trusted is None or len(lineage.revisions) > len(trusted.revisions):
                    longest_lineage_by_identity[identity] = lineage
            except OutcomeLineageTrustError as exc:
                raise ValueError("run registry contains conflicting outcome lineage evidence") from exc
        return raw

    def _read(self) -> dict:
        return self._read_existing()

    def _write(self, raw: dict) -> None:
        atomic_write_json(self.path, raw)
