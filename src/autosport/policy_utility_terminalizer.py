"""Fail-closed terminal journaling for unresolved owner-bound policy utility.

Schema-v1 :class:`PolicyUtilityEvidence` is deliberately caller-constructible and
contract-only.  It is therefore unsafe to append such an object to the canonical
``PolicyUtilityStore``: doing so would let an untrusted candidate permanently
reserve the causal semantic key before a future product-owned resolver can issue
canonical utility evidence.

This module keeps the canonical utility store untouched.  It journals only a
minimal, explicitly NON-AUTHORITATIVE terminal record in a deterministic sidecar
file.  The sidecar records that a candidate was BLOCKED/INCONCLUSIVE and preserves
its digest for audit/retry purposes; it is never utility evidence and cannot
authorize learning, retest, promotion, or successor activation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .integrity import durable_path_lock
from .learning_environment import Action, RewardEvidence, Transition
from .policy_update_authority import (
    UtilityBoundUpdateEvidence,
    attempt_utility_bound_update,
)
from .policy_utility_evidence import (
    PolicyUtilityError,
    PolicyUtilityEvidence,
    UtilityCompleteness,
)
from .transparent_bandit_policy import BanditPolicyState


_TERMINAL_SCHEMA = "autosport.policy_utility_unresolved_terminal"
_TERMINAL_SCHEMA_VERSION = 1
_TERMINAL_AUTHORITY = "NON_AUTHORITATIVE_SCHEMA_V1_CANDIDATE"


class PolicyUtilityTerminalizationError(PolicyUtilityError):
    """Raised when an unresolved utility terminal cannot be trusted."""


class TerminalizationDisposition(StrEnum):
    """Learning terminal states permitted while utility authority is unresolved."""

    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class PolicyUtilityTerminalRecord:
    """Minimal durable audit record for one non-authoritative utility candidate.

    The record intentionally does *not* persist caller-provided model, strategy,
    goal, risk, bankroll, portfolio, cost, denominator, or authority references.
    Those fields become trustworthy only after a product-owned resolver re-reads
    their canonical durable sources.  ``candidate_*`` values are assertions for
    audit correlation only and never occupy the canonical utility store.
    """

    candidate_evidence_id: str
    candidate_semantic_key: str
    disposition: TerminalizationDisposition
    authority_status: str = _TERMINAL_AUTHORITY

    def __post_init__(self) -> None:
        _sha256(self.candidate_evidence_id, "candidate_evidence_id")
        _sha256(self.candidate_semantic_key, "candidate_semantic_key")
        if type(self.disposition) is not TerminalizationDisposition:
            raise PolicyUtilityTerminalizationError(
                "disposition must be TerminalizationDisposition"
            )
        if self.authority_status != _TERMINAL_AUTHORITY:
            raise PolicyUtilityTerminalizationError(
                "unresolved terminal record cannot carry utility authority"
            )

    @property
    def record_id(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "schema": _TERMINAL_SCHEMA,
            "schema_version": _TERMINAL_SCHEMA_VERSION,
            "authority_status": self.authority_status,
            "candidate_evidence_id": self.candidate_evidence_id,
            "candidate_semantic_key": self.candidate_semantic_key,
            "disposition": self.disposition.value,
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["record_id"] = self.record_id
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PolicyUtilityTerminalRecord":
        expected = {
            "schema",
            "schema_version",
            "authority_status",
            "candidate_evidence_id",
            "candidate_semantic_key",
            "disposition",
            "record_id",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise PolicyUtilityTerminalizationError(
                "unresolved terminal record has unexpected fields"
            )
        if raw["schema"] != _TERMINAL_SCHEMA:
            raise PolicyUtilityTerminalizationError("unsupported terminal record schema")
        if raw["schema_version"] != _TERMINAL_SCHEMA_VERSION:
            raise PolicyUtilityTerminalizationError(
                "unsupported terminal record schema_version"
            )
        try:
            disposition = TerminalizationDisposition(raw["disposition"])
        except (TypeError, ValueError) as exc:
            raise PolicyUtilityTerminalizationError(
                "unsupported terminal record disposition"
            ) from exc
        record = cls(
            candidate_evidence_id=_string(raw["candidate_evidence_id"], "candidate_evidence_id"),
            candidate_semantic_key=_string(
                raw["candidate_semantic_key"], "candidate_semantic_key"
            ),
            disposition=disposition,
            authority_status=_string(raw["authority_status"], "authority_status"),
        )
        supplied_record_id = _string(raw["record_id"], "record_id")
        _sha256(supplied_record_id, "record_id")
        if supplied_record_id != record.record_id:
            raise PolicyUtilityTerminalizationError(
                "unresolved terminal record digest mismatch"
            )
        return record


@dataclass(frozen=True, slots=True)
class PolicyUtilityTerminalReceipt:
    """Acknowledgement for one NON-AUTHORITATIVE terminal journal record."""

    evidence_id: str
    semantic_key: str
    disposition: TerminalizationDisposition
    persisted: bool
    record_id: str

    def __post_init__(self) -> None:
        _sha256(self.evidence_id, "evidence_id")
        _sha256(self.semantic_key, "semantic_key")
        _sha256(self.record_id, "record_id")
        if type(self.disposition) is not TerminalizationDisposition:
            raise PolicyUtilityTerminalizationError(
                "disposition must be TerminalizationDisposition"
            )
        if type(self.persisted) is not bool:
            raise PolicyUtilityTerminalizationError("persisted must be bool")


@dataclass(frozen=True, slots=True)
class PolicyUtilityBlockedLearningReceipt:
    """One non-authoritative terminal plus unchanged governed-policy witness."""

    terminal: PolicyUtilityTerminalReceipt
    update_evidence: UtilityBoundUpdateEvidence
    champion_policy_id: str

    def __post_init__(self) -> None:
        if type(self.terminal) is not PolicyUtilityTerminalReceipt:
            raise PolicyUtilityTerminalizationError(
                "terminal must be exact PolicyUtilityTerminalReceipt"
            )
        if type(self.update_evidence) is not UtilityBoundUpdateEvidence:
            raise PolicyUtilityTerminalizationError(
                "update_evidence must be exact UtilityBoundUpdateEvidence"
            )
        _sha256(self.champion_policy_id, "champion_policy_id")
        if not self.update_evidence.reason_codes:
            raise PolicyUtilityTerminalizationError(
                "blocked learning receipt requires a fail-closed reason"
            )
        if self.update_evidence.predecessor_policy_id != self.champion_policy_id:
            raise PolicyUtilityTerminalizationError(
                "blocked update predecessor must be the champion policy"
            )
        if self.update_evidence.successor_policy_id != self.champion_policy_id:
            raise PolicyUtilityTerminalizationError(
                "blocked utility cannot change champion policy identity"
            )
        if self.update_evidence.utility_evidence_id != self.terminal.evidence_id:
            raise PolicyUtilityTerminalizationError(
                "blocked update utility evidence does not match terminal assertion"
            )
        if self.update_evidence.utility_semantic_key != self.terminal.semantic_key:
            raise PolicyUtilityTerminalizationError(
                "blocked update utility semantic key does not match terminal assertion"
            )


def _string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise PolicyUtilityTerminalizationError(f"{label} must be a non-empty string")
    return value


def _sha256(value: object, label: str) -> str:
    text = _string(value, label)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PolicyUtilityTerminalizationError(
            f"{label} must be lowercase SHA-256"
        )
    return text


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _journal_path_for(utility_store_path: Path) -> Path:
    """Return a path that can never be mistaken for the canonical utility store."""

    return utility_store_path.with_name(f"{utility_store_path.name}.unresolved-terminal.jsonl")


def _load_records(path: Path) -> tuple[PolicyUtilityTerminalRecord, ...]:
    if not path.exists():
        return ()
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise PolicyUtilityTerminalizationError(
            "cannot read unresolved utility terminal journal"
        ) from exc
    if raw_bytes and not raw_bytes.endswith(b"\n"):
        raise PolicyUtilityTerminalizationError(
            "unresolved terminal journal lacks canonical trailing record boundary"
        )

    records: list[PolicyUtilityTerminalRecord] = []
    seen: dict[str, PolicyUtilityTerminalRecord] = {}
    for line_number, raw_line in enumerate(raw_bytes.splitlines(), start=1):
        if not raw_line:
            raise PolicyUtilityTerminalizationError(
                f"empty unresolved terminal record at line {line_number}"
            )
        try:
            raw = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PolicyUtilityTerminalizationError(
                f"invalid unresolved terminal record at line {line_number}"
            ) from exc
        if type(raw) is not dict:
            raise PolicyUtilityTerminalizationError(
                f"unresolved terminal record {line_number} must be an object"
            )
        record = PolicyUtilityTerminalRecord.from_dict(raw)
        prior = seen.get(record.record_id)
        if prior is not None:
            if prior != record:
                raise PolicyUtilityTerminalizationError(
                    "unresolved terminal record_id collision"
                )
            raise PolicyUtilityTerminalizationError(
                "duplicate unresolved terminal record_id in durable journal"
            )
        seen[record.record_id] = record
        records.append(record)
    return tuple(records)


def _encode_records(records: tuple[PolicyUtilityTerminalRecord, ...]) -> bytes:
    return b"".join(
        (
            json.dumps(
                record.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        for record in records
    )


def _durable_replace(source: Path, destination: Path) -> None:
    """Publish a complete image with platform-appropriate metadata durability."""

    if os.name == "nt":
        # MoveFileExW with WRITE_THROUGH is the Windows equivalent of publishing
        # the directory entry synchronously; Python's os.replace alone does not
        # expose that durability flag.
        import ctypes

        MOVEFILE_REPLACE_EXISTING = 0x1
        MOVEFILE_WRITE_THROUGH = 0x8
        move_file_ex = ctypes.windll.kernel32.MoveFileExW
        move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file_ex.restype = ctypes.c_int
        if not move_file_ex(
            str(source),
            str(destination),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return

    os.replace(source, destination)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(destination.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _append_record(path: Path, record: PolicyUtilityTerminalRecord) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    with durable_path_lock(path):
        existing = _load_records(path)
        for prior in existing:
            if prior.record_id == record.record_id:
                return False

        successor = existing + (record,)
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
                handle.write(_encode_records(successor))
                handle.flush()
                os.fsync(handle.fileno())
            _durable_replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

        reloaded = {item.record_id: item for item in _load_records(path)}
        if reloaded.get(record.record_id) != record:
            raise PolicyUtilityTerminalizationError(
                "published unresolved terminal failed exact reload verification"
            )
        return True


class PolicyUtilityTerminalizer:
    """Journal unresolved utility candidates without granting utility authority."""

    def __init__(self, utility_store_path: str | Path) -> None:
        self._utility_store_path = Path(utility_store_path)
        self._journal_path = _journal_path_for(self._utility_store_path)

    @classmethod
    def from_path(cls, path: str | Path) -> "PolicyUtilityTerminalizer":
        """Bind the terminalizer to a canonical utility-store path without writing it."""

        return cls(path)

    @property
    def journal_path(self) -> Path:
        return self._journal_path

    @staticmethod
    def classify(evidence: PolicyUtilityEvidence) -> TerminalizationDisposition:
        """Purely classify an unresolved candidate; this grants no durable authority."""

        if type(evidence) is not PolicyUtilityEvidence:
            raise TypeError("evidence must be PolicyUtilityEvidence")
        if evidence.policy_update_eligible or evidence.source_resolved:
            raise PolicyUtilityTerminalizationError(
                "schema-v1 terminalizer cannot consume positive policy authority"
            )
        if evidence.completeness is UtilityCompleteness.INCOMPLETE:
            return TerminalizationDisposition.BLOCKED
        if evidence.completeness is UtilityCompleteness.UNSUPPORTED:
            return TerminalizationDisposition.INCONCLUSIVE
        raise PolicyUtilityTerminalizationError(
            "unsupported utility completeness for terminalization"
        )

    def terminalize(
        self, evidence: PolicyUtilityEvidence
    ) -> PolicyUtilityTerminalReceipt:
        """Journal a NON-AUTHORITATIVE candidate without touching PolicyUtilityStore.

        The candidate digest is assertion-only.  A later product-owned resolver is
        free to issue canonical evidence for the same causal semantic key because
        this method never appends to ``self._utility_store_path``.
        """

        disposition = self.classify(evidence)
        record = PolicyUtilityTerminalRecord(
            candidate_evidence_id=evidence.evidence_id,
            candidate_semantic_key=evidence.semantic_key,
            disposition=disposition,
        )
        persisted = _append_record(self._journal_path, record)
        return PolicyUtilityTerminalReceipt(
            evidence_id=evidence.evidence_id,
            semantic_key=evidence.semantic_key,
            disposition=disposition,
            persisted=persisted,
            record_id=record.record_id,
        )

    def terminalize_blocked_update(
        self,
        *,
        policy: BanditPolicyState,
        action: Action,
        reward: RewardEvidence,
        transition: Transition,
        utility: PolicyUtilityEvidence,
    ) -> PolicyUtilityBlockedLearningReceipt:
        """Validate a causal blocked update, then journal only its candidate digest."""

        for value, expected, label in (
            (policy, BanditPolicyState, "policy"),
            (action, Action, "action"),
            (reward, RewardEvidence, "reward"),
            (transition, Transition, "transition"),
            (utility, PolicyUtilityEvidence, "utility"),
        ):
            if type(value) is not expected:
                raise TypeError(f"{label} must be exact {expected.__name__}")

        successor, update_evidence = attempt_utility_bound_update(
            policy=policy,
            action=action,
            reward=reward,
            transition=transition,
            utility=utility,
        )
        if successor != policy:
            raise PolicyUtilityTerminalizationError(
                "non-authoritative utility unexpectedly mutated policy"
            )
        if not update_evidence.reason_codes:
            raise PolicyUtilityTerminalizationError(
                "non-authoritative utility unexpectedly produced positive update authority"
            )

        terminal = self.terminalize(utility)
        return PolicyUtilityBlockedLearningReceipt(
            terminal=terminal,
            update_evidence=update_evidence,
            champion_policy_id=policy.policy_id,
        )

    def resolve(self, evidence_id: str) -> PolicyUtilityTerminalRecord:
        """Resolve a non-authoritative terminal record by candidate evidence digest."""

        _sha256(evidence_id, "evidence_id")
        matches = [
            record
            for record in _load_records(self._journal_path)
            if record.candidate_evidence_id == evidence_id
        ]
        if len(matches) != 1:
            if not matches:
                raise KeyError(evidence_id)
            raise PolicyUtilityTerminalizationError(
                "candidate evidence_id resolves to multiple terminal records"
            )
        return matches[0]

    def records(self) -> tuple[PolicyUtilityTerminalRecord, ...]:
        """Return exact non-authoritative journal records in durable order."""

        return _load_records(self._journal_path)


__all__ = [
    "PolicyUtilityBlockedLearningReceipt",
    "PolicyUtilityTerminalReceipt",
    "PolicyUtilityTerminalRecord",
    "PolicyUtilityTerminalizationError",
    "PolicyUtilityTerminalizer",
    "TerminalizationDisposition",
]
