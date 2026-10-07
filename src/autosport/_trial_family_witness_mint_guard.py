"""Reserve trial-family cross-ledger witness publication to product authority.

The trial journal must recognize SEQUENTIAL_LOOK_REGISTERED during replay, but the
legacy generic _append_event seam must never let a caller mint that authority. The
cross-ledger publisher is the only supported writer because it first proves exact
durable native sequential evidence and captures the canonical registry prefix under
the shared workspace lock.

Promotion eligibility also composes trial-family, sequential, and registry truth.
Keep that entire cross-ledger assertion under the same workspace lock so a normal
concurrent attempt publication cannot land between its precondition read and witness
re-resolution.

Keep the pre-guard capabilities closure-local. A module-global reference would
itself be an importable authority bypass once the reserved event kind is recognized
by replay.
"""

from __future__ import annotations

from typing import Any, Callable

from . import trial_family_accounting as _tfa
from . import _trial_family_cross_ledger_witness as _witness
from .workspace_lock import WorkspaceEconomicLock


def _install_reserved_witness_mint_guard() -> None:
    original_append_event = _tfa.TrialFamilyAccountingStore._append_event
    original_assert_promotion = (
        _tfa.TrialFamilyAccountingStore.assert_promotion_evidence_eligible
    )
    workspace_lock = WorkspaceEconomicLock

    def append_event_without_reserved_witness_mint(
        self: _tfa.TrialFamilyAccountingStore,
        kind: str,
        event_at: str,
        payload: dict[str, Any],
        *,
        locked_payload_factory: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        if kind == _witness._WITNESS_KIND:
            raise ValueError(
                "SEQUENTIAL_LOOK_REGISTERED is reserved for product-owned cross-ledger witness publication"
            )
        return original_append_event(
            self,
            kind,
            event_at,
            payload,
            locked_payload_factory=locked_payload_factory,
        )

    def assert_promotion_evidence_eligible_linearized(
        self: _tfa.TrialFamilyAccountingStore,
        *,
        evidence: Any,
        registry: Any,
        accounted_attempt_count: int,
    ) -> _tfa.TrialFamilySnapshot:
        # The wrapped cross-ledger assertion performs multiple authority reads by
        # design. Serialize the whole composition, not merely each individual read,
        # so public writers using the same workspace lock cannot create a TOCTOU
        # between the attempt-count/open-attempt check and witness verification.
        # Resolve the lock capability from this install-time closure, not a mutable
        # module global that a caller could rebind before an authority-bearing read.
        with workspace_lock(self.workspace_root):
            return original_assert_promotion(
                self,
                evidence=evidence,
                registry=registry,
                accounted_attempt_count=accounted_attempt_count,
            )

    _tfa.TrialFamilyAccountingStore._append_event = append_event_without_reserved_witness_mint
    _tfa.TrialFamilyAccountingStore.assert_promotion_evidence_eligible = (
        assert_promotion_evidence_eligible_linearized
    )


_install_reserved_witness_mint_guard()
del _install_reserved_witness_mint_guard

__all__: list[str] = []
