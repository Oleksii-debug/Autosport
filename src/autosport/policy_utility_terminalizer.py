"""Exactly-once terminalization for owner-bound policy utility evidence.

This module is deliberately a narrow downstream seam.  It does not calculate
utility, update a policy, promote a challenger, or create a second evidence
store.  It consumes the existing :mod:`policy_utility_evidence` contract and
durably closes the only dispositions that are safe before the product-owned
economic resolver is available: ``BLOCKED`` and ``INCONCLUSIVE``.

The production composition root can call this after settlement/campaign
finalization.  A later authority may add a positive resolver without changing
the idempotent blocked-evidence path below.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .policy_utility_evidence import (
    PolicyUtilityError,
    PolicyUtilityEvidence,
    PolicyUtilityStore,
    UtilityCompleteness,
)


class PolicyUtilityTerminalizationError(PolicyUtilityError):
    """Raised when a terminal learning disposition cannot be trusted."""


class TerminalizationDisposition(StrEnum):
    """Durable learning terminal states exposed to the campaign owner."""

    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class PolicyUtilityTerminalReceipt:
    """Immutable acknowledgement of one durable terminal evidence record.

    ``persisted`` is true only for the first append.  A false value means the
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


class PolicyUtilityTerminalizer:
    """Close incomplete policy utility evidence exactly once.

    The terminalizer reuses ``PolicyUtilityStore`` as the single durable
    authority.  It intentionally accepts only schema-v1 evidence whose
    completeness is already ``INCOMPLETE`` or ``UNSUPPORTED``; those records
    cannot carry positive policy authority and therefore safely preserve the
    current champion.  Complete utility resolution and promotion remain the
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

        The append operation is replay-safe: the same causal semantic key and
        digest return ``persisted=False`` on retry, while changed evidence for
        that key fails closed inside ``PolicyUtilityStore``.
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

        persisted = self._store.append(evidence)
        return PolicyUtilityTerminalReceipt(
            evidence_id=evidence.evidence_id,
            semantic_key=evidence.semantic_key,
            disposition=disposition,
            persisted=persisted,
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
    "PolicyUtilityTerminalReceipt",
    "PolicyUtilityTerminalizationError",
    "PolicyUtilityTerminalizer",
    "TerminalizationDisposition",
]
