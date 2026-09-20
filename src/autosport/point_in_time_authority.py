from __future__ import annotations

"""Point-in-time authority compatibility surface with monotonic holdout fencing.

The implementation accumulated on the EXTVAL candidate remains intact in the
private legacy module.  This surface tightens the one durability boundary that
cannot be expressed by the rollbackable ledger/head pair alone: every new
holdout consumption first publishes an independent content-addressed marker.
Restoring an older, otherwise-valid ledger+anchor pair therefore fails closed
instead of making already-consumed evidence look fresh again.
"""

from pathlib import Path
from typing import Final

from . import _point_in_time_authority_legacy as _legacy
from ._point_in_time_authority_legacy import *  # noqa: F403
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .workspace_lock import WorkspaceEconomicLock


class HoldoutConsumptionLedger(_legacy.HoldoutConsumptionLedger):
    """Holdout ledger whose consumed identities survive paired head rollback."""

    MONOTONIC_DIR_NAME: Final = "holdout_consumption_immutable"

    def __init__(
        self,
        workspace: str | Path,
        *,
        scientific_registry: _legacy.ScientificRegistry,
    ) -> None:
        super().__init__(workspace, scientific_registry=scientific_registry)
        self.monotonic_dir = self.workspace / self.MONOTONIC_DIR_NAME

    def _marker_path(self, holdout_access_id: str) -> Path:
        canonical = _legacy._sha256(holdout_access_id, "holdout_access_id")
        return self.monotonic_dir / f"{canonical}.json"

    @staticmethod
    def _read_marker(path: Path) -> _legacy.HoldoutConsumptionReceipt:
        try:
            raw = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise _legacy.HoldoutConsumptionError(
                "invalid immutable holdout-consumption marker"
            ) from exc
        try:
            receipt = _legacy.HoldoutConsumptionReceipt.from_payload(raw)
        except _legacy.HoldoutConsumptionError as exc:
            raise _legacy.HoldoutConsumptionError(
                "invalid immutable holdout-consumption marker"
            ) from exc
        if path.stem != receipt.holdout_access_id:
            raise _legacy.HoldoutConsumptionError(
                "immutable holdout-consumption marker identity mismatch"
            )
        return receipt

    def _markers_unlocked(self) -> tuple[_legacy.HoldoutConsumptionReceipt, ...]:
        if not self.monotonic_dir.exists():
            return ()
        if not self.monotonic_dir.is_dir():
            raise _legacy.HoldoutConsumptionError(
                "immutable holdout-consumption authority is not a directory"
            )
        markers = tuple(
            self._read_marker(path)
            for path in sorted(self.monotonic_dir.glob("*.json"))
        )
        seen: set[str] = set()
        for marker in markers:
            if marker.holdout_access_id in seen:
                raise _legacy.HoldoutConsumptionError(
                    "duplicate immutable holdout-consumption marker"
                )
            seen.add(marker.holdout_access_id)
        return markers

    def _assert_markers_match_state(self, state: _legacy._LedgerState) -> None:
        receipts = {receipt.holdout_access_id: receipt for receipt in state.receipts}
        for marker in self._markers_unlocked():
            current = receipts.get(marker.holdout_access_id)
            if current is None or current.receipt_sha256 != marker.receipt_sha256:
                raise _legacy.HoldoutConsumptionError(
                    "holdout ledger rollback detected against immutable consumption marker"
                )

    def _load_unlocked(self) -> _legacy._LedgerState:
        state = super()._load_unlocked()
        self._assert_markers_match_state(state)
        return state

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
        """Consume once, publishing non-rollbackable identity before pair update."""

        _legacy._require_registered_dataset(self.scientific_registry, dataset_snapshot)
        _legacy._validate_holdout_available(dataset_snapshot, consumed_at=consumed_at)
        access_id = _legacy.holdout_identity(
            scientific_registry=self.scientific_registry,
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=research_protocol_id,
            confirmation_trial_family_id=confirmation_trial_family_id,
        )
        marker_path = self._marker_path(access_id)

        with WorkspaceEconomicLock(self.workspace):
            current = super()._load_unlocked()
            self._assert_markers_match_state(current)
            if marker_path.exists() or any(
                receipt.holdout_access_id == access_id for receipt in current.receipts
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

            self.monotonic_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(marker_path, receipt.to_payload())
            marker = self._read_marker(marker_path)
            if marker != receipt:
                raise _legacy.HoldoutConsumptionError(
                    "immutable holdout-consumption marker did not verify after write"
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
            atomic_write_json(self.path, next_payload)
            atomic_write_json(self.anchor_path, self._anchor_payload(next_state))
            verified = self._load_unlocked()
            if verified != next_state:
                raise _legacy.HoldoutConsumptionError(
                    "published holdout ledger did not verify after write"
                )
            return receipt


__all__ = list(_legacy.__all__)
