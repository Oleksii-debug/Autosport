from __future__ import annotations

"""Bind point-in-time holdout consumption to the shared machine authority.

The point-in-time candidate predates the canonical MonotonicWorkspaceAuthority
and originally carried a workspace-local immutable-marker fence.  That marker
lives in the same rollback/deletion domain as the ledger, so it cannot be the
final anti-rollback authority.  This compatibility installer keeps the public
point-in-time API intact while routing durable holdout generations through the
single shared machine-state authority merged by #637.
"""

import os
from pathlib import Path
from typing import Final

from . import _point_in_time_authority_legacy as _legacy
from . import point_in_time_authority as _public
from .integrity import atomic_write_json
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
    RecoveryDisposition,
)
from .workspace_lock import WorkspaceEconomicLock


_DOMAIN: Final = "data.point-in-time-holdout-consumption"
_KEY: Final = "holdout-consumption-ledger-v1"


class HoldoutConsumptionLedger(_public.HoldoutConsumptionLedger):
    """Holdout ledger fenced by the canonical shared monotonic authority."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        scientific_registry: _legacy.ScientificRegistry,
        monotonic_authority_root: str | Path | None = None,
    ) -> None:
        # Do not initialize the predecessor's workspace-local marker subsystem.
        # Keep one rollback authority only: MonotonicWorkspaceAuthority.
        workspace_path = Path(
            os.path.abspath(os.fspath(Path(workspace).expanduser()))
        )
        _legacy.HoldoutConsumptionLedger.__init__(
            self,
            workspace_path,
            scientific_registry=scientific_registry,
        )
        self._monotonic_authority_root = monotonic_authority_root

    def _authority(self) -> MonotonicWorkspaceAuthority:
        return MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain=_DOMAIN,
            key=_KEY,
            authority_root=self._monotonic_authority_root,
        )

    def _observed_digest(self, state: _legacy._LedgerState) -> str | None:
        if state.generation == 0:
            if self.path.exists() or self.anchor_path.exists():
                raise _legacy.HoldoutConsumptionError(
                    "pristine holdout state unexpectedly has persisted ledger bytes"
                )
            return None
        return state.ledger_sha256

    @staticmethod
    def _semantic_binding(
        *,
        state: _legacy._LedgerState,
        receipt: _legacy.HoldoutConsumptionReceipt,
    ) -> str:
        return _legacy._digest(
            {
                "schema": "autosport.holdout_consumption.monotonic_binding",
                "schema_version": 1,
                "domain": _DOMAIN,
                "key": _KEY,
                "generation": state.generation,
                "ledger_sha256": state.ledger_sha256,
                "holdout_access_id": receipt.holdout_access_id,
                "receipt_sha256": receipt.receipt_sha256,
            }
        )

    @staticmethod
    def _pending_record(authority: MonotonicWorkspaceAuthority):
        history = authority.read_history()
        if history and history[-1].phase is AuthorityPhase.PREPARE:
            return history[-1]
        return None

    def _repair_interrupted_pair_unlocked(
        self,
        authority: MonotonicWorkspaceAuthority,
    ) -> _legacy._LedgerState | None:
        """Repair only the canonical PREPARE -> ledger -> anchor crash prefix."""

        pending = self._pending_record(authority)
        if pending is None or not self.path.exists():
            return None
        try:
            ledger_text = self.path.read_text(encoding="utf-8")
            candidate = self._decode_ledger(ledger_text)
        except (OSError, _legacy.HoldoutConsumptionError):
            return None
        if (
            candidate.generation != pending.generation
            or candidate.ledger_sha256 != pending.intended_state_sha256
        ):
            return None

        # The independent PREPARE already committed the exact intended digest.
        # If publication crashed between the two local atomic replacements, the
        # anchor is mechanically derivable from those exact intended bytes.
        atomic_write_json(self.anchor_path, self._anchor_payload(candidate))
        return _legacy.HoldoutConsumptionLedger._load_unlocked(self)

    def _load_local_unlocked(
        self,
        authority: MonotonicWorkspaceAuthority,
    ) -> _legacy._LedgerState:
        try:
            return _legacy.HoldoutConsumptionLedger._load_unlocked(self)
        except _legacy.HoldoutConsumptionError:
            repaired = self._repair_interrupted_pair_unlocked(authority)
            if repaired is not None:
                return repaired
            raise

    def _recover_authority_unlocked(
        self,
        authority: MonotonicWorkspaceAuthority,
        state: _legacy._LedgerState,
    ) -> _legacy._LedgerState:
        observed = self._observed_digest(state)
        try:
            pending = self._pending_record(authority)
            if pending is not None and observed == pending.intended_state_sha256:
                recovery = authority.recover(
                    observed_state_sha256=observed,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=pending.semantic_binding_sha256,
                )
            else:
                recovery = authority.recover(observed_state_sha256=observed)
        except MonotonicWorkspaceAuthorityError as exc:
            raise _legacy.HoldoutConsumptionError(
                "holdout ledger rollback/deletion rejected by shared monotonic authority"
            ) from exc

        if recovery.disposition is RecoveryDisposition.PRISTINE:
            if state.generation != 0 or observed is not None:
                raise _legacy.HoldoutConsumptionError(
                    "shared monotonic authority reported pristine for persisted holdout state"
                )
            return state

        if (
            recovery.committed_generation != state.generation
            or recovery.committed_state_sha256 != observed
        ):
            raise _legacy.HoldoutConsumptionError(
                "holdout ledger generation disagrees with shared monotonic authority"
            )
        return state

    def _load_unlocked(self) -> _legacy._LedgerState:
        try:
            authority = self._authority()
            local = self._load_local_unlocked(authority)
            return self._recover_authority_unlocked(authority, local)
        except MonotonicWorkspaceAuthorityError as exc:
            raise _legacy.HoldoutConsumptionError(
                "cannot validate shared monotonic holdout authority"
            ) from exc

    def receipts(self) -> tuple[_legacy.HoldoutConsumptionReceipt, ...]:
        with WorkspaceEconomicLock(self.workspace):
            return self._load_unlocked().receipts

    def receipt_for(
        self,
        holdout_access_id: str,
    ) -> _legacy.HoldoutConsumptionReceipt | None:
        target = _legacy._sha256(holdout_access_id, "holdout_access_id")
        with WorkspaceEconomicLock(self.workspace):
            for receipt in self._load_unlocked().receipts:
                if receipt.holdout_access_id == target:
                    return receipt
        return None

    def is_consumed(
        self,
        *,
        dataset_snapshot: _legacy.DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
    ) -> bool:
        access_id = _legacy.holdout_identity(
            scientific_registry=self.scientific_registry,
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=research_protocol_id,
            confirmation_trial_family_id=confirmation_trial_family_id,
        )
        return self.receipt_for(access_id) is not None

    def consume(
        self,
        *,
        dataset_snapshot: _legacy.DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
        consumer_identity: str,
        purpose: str,
        consumed_at: _legacy.datetime,
    ) -> _legacy.HoldoutConsumptionReceipt:
        """Consume once with PREPARE -> local publish -> COMMIT durability."""

        _legacy._require_registered_dataset(self.scientific_registry, dataset_snapshot)
        _legacy._validate_holdout_available(dataset_snapshot, consumed_at=consumed_at)
        access_id = _legacy.holdout_identity(
            scientific_registry=self.scientific_registry,
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=research_protocol_id,
            confirmation_trial_family_id=confirmation_trial_family_id,
        )

        with WorkspaceEconomicLock(self.workspace):
            authority = self._authority()
            current = self._recover_authority_unlocked(
                authority,
                self._load_local_unlocked(authority),
            )
            if any(
                receipt.holdout_access_id == access_id
                for receipt in current.receipts
            ):
                raise _legacy.HoldoutAlreadyConsumedError(
                    "confirmation/holdout evidence was already consumed"
                )

            receipt = _legacy.HoldoutConsumptionReceipt.create(
                holdout_access_id=access_id,
                dataset_snapshot=dataset_snapshot,
                research_protocol_id=research_protocol_id,
                confirmation_trial_family_id=confirmation_trial_family_id,
                consumer_identity=consumer_identity,
                purpose=purpose,
                consumed_at=consumed_at,
            )
            next_payload = self._payload(
                current.receipts + (receipt,),
                generation=current.generation + 1,
                previous_ledger_sha256=current.ledger_sha256,
            )
            next_state = _legacy._LedgerState(
                receipts=current.receipts + (receipt,),
                generation=current.generation + 1,
                ledger_sha256=next_payload["ledger_sha256"],
            )
            binding = self._semantic_binding(state=next_state, receipt=receipt)
            tx_id = (
                f"holdout:{next_state.generation}:{receipt.holdout_access_id}"
            )

            try:
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=self._observed_digest(current),
                    intended_state_sha256=next_state.ledger_sha256,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise _legacy.HoldoutConsumptionError(
                    "shared monotonic authority rejected holdout PREPARE"
                ) from exc

            atomic_write_json(self.path, next_payload)
            atomic_write_json(self.anchor_path, self._anchor_payload(next_state))
            verified = _legacy.HoldoutConsumptionLedger._load_unlocked(self)
            if verified != next_state:
                raise _legacy.HoldoutConsumptionError(
                    "published holdout ledger did not verify after write"
                )

            try:
                authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=next_state.ledger_sha256,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise _legacy.HoldoutConsumptionError(
                    "shared monotonic authority rejected holdout COMMIT"
                ) from exc

            final_state = self._recover_authority_unlocked(authority, verified)
            if final_state != next_state:
                raise _legacy.HoldoutConsumptionError(
                    "committed holdout state failed shared monotonic verification"
                )
            return receipt


HoldoutConsumptionLedger.__module__ = _public.__name__

if not getattr(_public, "_shared_monotonic_holdout_installed", False):
    _public.HoldoutConsumptionLedger = HoldoutConsumptionLedger
    _public._shared_monotonic_holdout_installed = True
