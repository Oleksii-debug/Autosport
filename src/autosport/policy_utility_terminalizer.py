"""Exactly-once terminalization for owner-bound policy utility evidence.

This module is deliberately a narrow downstream seam. It does not calculate
utility, promote a challenger, or create a second evidence store. It consumes
the existing :mod:`policy_utility_evidence` contract and durably closes the
only dispositions that are safe while source-resolved complete net economics
is unavailable: ``BLOCKED`` and ``INCONCLUSIVE``.

The blocked product path also composes the existing utility-bound update gate,
so raw ``RewardEvidence`` can never mutate a policy before the terminal record
is durable. A later source-resolved positive authority can extend the same
boundary without weakening this replay-safe champion-preserving path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
import tempfile

from .integrity import durable_path_lock
from .learning_environment import Action, RewardEvidence, Transition
from .policy_update_authority import (
    UtilityBoundUpdateEvidence,
    attempt_utility_bound_update,
)
from .policy_utility_evidence import (
    PolicyUtilityError,
    PolicyUtilityEvidence,
    PolicyUtilityStore,
    UtilityCompleteness,
)
from .transparent_bandit_policy import BanditPolicyState


class PolicyUtilityTerminalizationError(PolicyUtilityError):
    """Raised when a terminal learning disposition cannot be trusted."""


class TerminalizationDisposition(StrEnum):
    """Durable learning terminal states exposed to the campaign owner."""

    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class PolicyUtilityTerminalReceipt:
    """Immutable acknowledgement of one durable terminal evidence record.

    ``persisted`` is true only for the first append. A false value means the
    exact same causal evidence was already present after retry/restart; it is
    not permission to invoke a learner or promotion path again.
    """

    evidence_id: str
    semantic_key: str
    disposition: TerminalizationDisposition
    persisted: bool

    def __post_init__(self) -> None:
        if type(self.evidence_id) is not str or len(self.evidence_id) != 64:
            raise PolicyUtilityTerminalizationError("evidence_id must be a SHA-256 digest")
        if any(char not in "0123456789abcdef" for char in self.evidence_id):
            raise PolicyUtilityTerminalizationError("evidence_id must be lowercase SHA-256")
        if type(self.semantic_key) is not str or len(self.semantic_key) != 64:
            raise PolicyUtilityTerminalizationError("semantic_key must be a SHA-256 digest")
        if any(char not in "0123456789abcdef" for char in self.semantic_key):
            raise PolicyUtilityTerminalizationError("semantic_key must be lowercase SHA-256")
        if type(self.disposition) is not TerminalizationDisposition:
            raise PolicyUtilityTerminalizationError(
                "disposition must be TerminalizationDisposition"
            )
        if type(self.persisted) is not bool:
            raise PolicyUtilityTerminalizationError("persisted must be bool")


@dataclass(frozen=True, slots=True)
class PolicyUtilityBlockedLearningReceipt:
    """One durable blocked utility plus the unchanged governed-policy witness."""

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
        if type(self.champion_policy_id) is not str or len(self.champion_policy_id) != 64:
            raise PolicyUtilityTerminalizationError(
                "champion_policy_id must be a SHA-256 digest"
            )
        if any(char not in "0123456789abcdef" for char in self.champion_policy_id):
            raise PolicyUtilityTerminalizationError(
                "champion_policy_id must be lowercase SHA-256"
            )
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
                "blocked update utility evidence does not match durable terminal"
            )
        if self.update_evidence.utility_semantic_key != self.terminal.semantic_key:
            raise PolicyUtilityTerminalizationError(
                "blocked update utility semantic key does not match durable terminal"
            )


def _encoded_evidence_line(evidence: PolicyUtilityEvidence) -> bytes:
    return (
        json.dumps(
            evidence.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_compare_and_append(
    store: PolicyUtilityStore,
    evidence: PolicyUtilityEvidence,
) -> bool:
    """Cross-process, crash-safe compare-and-append to the canonical JSONL store.

    ``PolicyUtilityStore`` predates product-level exactly-once terminalization and
    documents cross-process fencing as a composition-root responsibility.  The
    terminalizer is now that product boundary, so it supplies the missing durable
    path fence and publishes a complete replacement image atomically.  A crash
    before ``os.replace`` leaves the previous canonical image authoritative; a
    crash after it leaves the complete successor image authoritative.  Temporary
    files are not evidence and are ignored on restart.
    """

    path = store.path
    path.parent.mkdir(parents=True, exist_ok=True)

    with durable_path_lock(path):
        existing = store.list()
        for prior in existing:
            if prior.semantic_key == evidence.semantic_key:
                if prior.evidence_id == evidence.evidence_id:
                    return False
                raise PolicyUtilityError(
                    "policy utility semantic drift for existing causal update key"
                )
            if prior.evidence_id == evidence.evidence_id:
                raise PolicyUtilityError("policy utility evidence_id collision")

        previous = path.read_bytes() if path.exists() else b""
        if previous and not previous.endswith(b"\n"):
            raise PolicyUtilityError(
                "policy utility store lacks canonical trailing record boundary"
            )

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
                handle.write(previous)
                handle.write(_encoded_evidence_line(evidence))
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

        persisted = store.get(evidence.evidence_id)
        if persisted != evidence:
            raise PolicyUtilityTerminalizationError(
                "published policy utility evidence failed exact reload verification"
            )
        return True


class PolicyUtilityTerminalizer:
    """Close incomplete policy utility evidence exactly once.

    The terminalizer reuses ``PolicyUtilityStore`` as the single durable
    authority. It intentionally accepts only schema-v1 evidence whose
    completeness is already ``INCOMPLETE`` or ``UNSUPPORTED``; those records
    cannot carry positive policy authority and therefore safely preserve the
    current champion. Complete utility resolution and promotion remain the
    responsibility of the canonical owner-bound utility authority.
    """

    def __init__(self, store: PolicyUtilityStore) -> None:
        if type(store) is not PolicyUtilityStore:
            raise TypeError("store must be PolicyUtilityStore")
        self._store = store

    @classmethod
    def from_path(cls, path: str | Path) -> "PolicyUtilityTerminalizer":
        """Build a terminalizer over the existing policy utility JSONL store."""

        return cls(PolicyUtilityStore(path))

    def terminalize(
        self, evidence: PolicyUtilityEvidence
    ) -> PolicyUtilityTerminalReceipt:
        """Persist one blocked/inconclusive terminal and return its receipt.

        The append operation is replay-safe across processes and restart: the same
        causal semantic key and digest return ``persisted=False`` on retry, while
        changed evidence for that key fails closed before publication.
        """

        if type(evidence) is not PolicyUtilityEvidence:
            raise TypeError("evidence must be PolicyUtilityEvidence")
        if evidence.policy_update_eligible or evidence.source_resolved:
            raise PolicyUtilityTerminalizationError(
                "terminalizer cannot consume positive policy authority"
            )

        if evidence.completeness is UtilityCompleteness.INCOMPLETE:
            disposition = TerminalizationDisposition.BLOCKED
        elif evidence.completeness is UtilityCompleteness.UNSUPPORTED:
            disposition = TerminalizationDisposition.INCONCLUSIVE
        else:  # pragma: no cover - exhaustive enum fence for future versions
            raise PolicyUtilityTerminalizationError(
                "unsupported utility completeness for terminalization"
            )

        persisted = _atomic_compare_and_append(self._store, evidence)
        return PolicyUtilityTerminalReceipt(
            evidence_id=evidence.evidence_id,
            semantic_key=evidence.semantic_key,
            disposition=disposition,
            persisted=persisted,
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
        """Durably close one non-authoritative utility without mutating policy.

        Exact product-facing types are required here even though lower-level
        library functions accept ``isinstance``. The existing utility gate
        validates causal binding and returns an unchanged policy plus an
        immutable blocked witness. Only after that witness is proven blocked is
        the utility evidence appended to the canonical durable store.

        Replaying the same delivery after a crash yields the same update witness
        and ``terminal.persisted == False``; it never grants permission to call
        retest, promotion, or next-episode successor logic.
        """

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

    def resolve(self, evidence_id: str) -> PolicyUtilityEvidence:
        """Resolve the exact durable terminal evidence after restart."""

        evidence = self._store.get(evidence_id)
        if evidence.policy_update_eligible or evidence.source_resolved:
            raise PolicyUtilityTerminalizationError(
                "durable evidence unexpectedly carries positive policy authority"
            )
        return evidence


__all__ = [
    "PolicyUtilityBlockedLearningReceipt",
    "PolicyUtilityTerminalReceipt",
    "PolicyUtilityTerminalizationError",
    "PolicyUtilityTerminalizer",
    "TerminalizationDisposition",
]
