from __future__ import annotations

"""Point-in-time authority compatibility surface with monotonic holdout fencing.

The implementation accumulated on the EXTVAL candidate remains intact in the
private legacy module.  This surface tightens the one durability boundary that
cannot be expressed by the rollbackable ledger/head pair alone: every new
holdout consumption first publishes an independent content-addressed marker.
Restoring an older, otherwise-valid ledger+anchor pair therefore fails closed
instead of making already-consumed evidence look fresh again.
"""

import hashlib
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Final

from . import _point_in_time_authority_legacy as _legacy
from ._point_in_time_authority_legacy import *  # noqa: F403
from .historical_snapshot import (
    HISTORICAL_CAPTURE_WITNESS_KIND,
    HistoricalSnapshotAuthority,
    resolve_historical_snapshot_authority,
)
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .workspace_lock import WorkspaceEconomicLock


class SourceRevisionAuthorityStore(_legacy.SourceRevisionAuthorityStore):
    """Source authority store with durable provider-capture provenance."""

    PROVIDER_CAPTURE_DIR_NAME: Final = "point_in_time_provider_capture_authority"
    PROVIDER_SOURCE_ID: Final = "parlayapi:table_tennis"

    def __init__(self, workspace: str | Path) -> None:
        super().__init__(workspace)
        self.provider_capture_dir = (
            self.workspace / self.PROVIDER_CAPTURE_DIR_NAME
        )
        self._verify_provider_state()

    @classmethod
    def _provider_policy(
        cls,
        *,
        revision_policy_id: str,
        frozen_at: datetime,
    ) -> _legacy.RevisionPolicyAuthority:
        payload = {
            "schema": "autosport.revision_availability_policy",
            "schema_version": 1,
            "source_identity": cls.PROVIDER_SOURCE_ID,
            "policy_version": "1",
            "witness_kind": HISTORICAL_CAPTURE_WITNESS_KIND,
            "availability_semantics": "source_as_of<=available_at",
        }
        return _legacy.RevisionPolicyAuthority.create(
            revision_policy_id=revision_policy_id,
            policy_content_json=_legacy._canonical_json(payload),
            frozen_at=frozen_at,
        )

    def _verify_provider_policy(
        self,
        policy: _legacy.RevisionPolicyAuthority,
    ) -> None:
        if not policy.witness_kind.startswith("provider-"):
            return
        if (
            policy.source_identity != self.PROVIDER_SOURCE_ID
            or policy.witness_kind != HISTORICAL_CAPTURE_WITNESS_KIND
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "unsupported provider revision policy authority"
            )
        expected = self._provider_policy(
            revision_policy_id=policy.revision_policy_id,
            frozen_at=policy.frozen_at,
        )
        if expected.authority_sha256 != policy.authority_sha256:
            raise _legacy.SourceRevisionAuthorityError(
                "provider revision policy is not canonical"
            )

    def register_policy(
        self,
        policy: _legacy.RevisionPolicyAuthority,
    ) -> str:
        if (
            isinstance(policy, _legacy.RevisionPolicyAuthority)
            and policy.witness_kind.startswith("provider-")
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "provider revision policy must be registered from canonical source policy"
            )
        return super().register_policy(policy)

    def register_provider_capture_policy(
        self,
        *,
        revision_policy_id: str,
        frozen_at: datetime,
    ) -> _legacy.RevisionPolicyAuthority:
        policy = self._provider_policy(
            revision_policy_id=revision_policy_id,
            frozen_at=frozen_at,
        )
        super().register_policy(policy)
        return policy

    def resolve_policy(
        self,
        revision_policy_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> _legacy.RevisionPolicyAuthority:
        policy = super().resolve_policy(
            revision_policy_id,
            expected_sha256=expected_sha256,
        )
        self._verify_provider_policy(policy)
        return policy

    def _capture_bundle_dir(self, availability_witness_id: str) -> Path:
        witness_id = _legacy._text(
            availability_witness_id,
            "availability_witness_id",
        )
        key = hashlib.sha256(witness_id.encode("utf-8")).hexdigest()
        return self.provider_capture_dir / key

    def _capture_bundle_paths(
        self,
        availability_witness_id: str,
    ) -> tuple[Path, Path]:
        root = self._capture_bundle_dir(availability_witness_id)
        return root / "market.jsonl", root / "evidence.json"

    @staticmethod
    def _expected_provider_witness(
        *,
        availability_witness_id: str,
        authority: HistoricalSnapshotAuthority,
        recorded_at: datetime,
    ) -> _legacy.AvailabilityWitnessAuthority:
        payload = {
            "schema": "autosport.source_availability_witness",
            "schema_version": 1,
            "source_identity": authority.source_identity,
            "source_revision": authority.source_revision,
            "source_revision_sha256": authority.source_revision_sha256,
            "witness_kind": authority.witness_kind,
            "source_as_of": authority.source_as_of,
            "available_at": authority.available_at,
        }
        return _legacy.AvailabilityWitnessAuthority.create(
            availability_witness_id=availability_witness_id,
            witness_content_json=_legacy._canonical_json(payload),
            recorded_at=recorded_at,
        )

    def _resolve_capture_bundle(
        self,
        availability_witness_id: str,
    ) -> HistoricalSnapshotAuthority:
        market_path, evidence_path = self._capture_bundle_paths(
            availability_witness_id
        )
        try:
            return resolve_historical_snapshot_authority(
                market_path=market_path,
                evidence_path=evidence_path,
            )
        except (OSError, ValueError) as exc:
            raise _legacy.SourceRevisionAuthorityError(
                "canonical provider capture authority is missing or invalid"
            ) from exc

    def _publish_capture_bundle(
        self,
        *,
        availability_witness_id: str,
        market_path: str | Path,
        evidence_path: str | Path,
        authority: HistoricalSnapshotAuthority,
    ) -> None:
        target = self._capture_bundle_dir(availability_witness_id)
        if target.exists():
            current = self._resolve_capture_bundle(availability_witness_id)
            if current.capture_sha256 != authority.capture_sha256:
                raise _legacy.SourceRevisionAuthorityError(
                    "conflicting immutable provider capture authority"
                )
            return

        self.provider_capture_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                dir=self.provider_capture_dir,
                prefix=".provider-capture-",
            )
        )
        try:
            for source, name in (
                (Path(market_path), "market.jsonl"),
                (Path(evidence_path), "evidence.json"),
            ):
                destination = temporary / name
                data = source.read_bytes()
                with destination.open("wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(temporary, target)
        except FileExistsError:
            # Another same-process/source-authority writer may have published the
            # identical content-addressed bundle first. Verify rather than replace.
            pass
        except OSError as exc:
            raise _legacy.SourceRevisionAuthorityError(
                "cannot publish canonical provider capture authority"
            ) from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

        current = self._resolve_capture_bundle(availability_witness_id)
        if current.capture_sha256 != authority.capture_sha256:
            raise _legacy.SourceRevisionAuthorityError(
                "published provider capture authority did not verify"
            )

    def register_witness(
        self,
        witness: _legacy.AvailabilityWitnessAuthority,
    ) -> str:
        if (
            isinstance(witness, _legacy.AvailabilityWitnessAuthority)
            and witness.witness_kind.startswith("provider-")
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "provider availability witness requires canonical persisted capture evidence"
            )
        return super().register_witness(witness)

    def register_provider_capture_witness(
        self,
        *,
        availability_witness_id: str,
        market_path: str | Path,
        evidence_path: str | Path,
        recorded_at: datetime,
    ) -> _legacy.AvailabilityWitnessAuthority:
        try:
            authority = resolve_historical_snapshot_authority(
                market_path=market_path,
                evidence_path=evidence_path,
            )
        except (OSError, ValueError) as exc:
            raise _legacy.SourceRevisionAuthorityError(
                "canonical provider capture evidence cannot be resolved"
            ) from exc
        witness = self._expected_provider_witness(
            availability_witness_id=availability_witness_id,
            authority=authority,
            recorded_at=recorded_at,
        )
        self._publish_capture_bundle(
            availability_witness_id=availability_witness_id,
            market_path=market_path,
            evidence_path=evidence_path,
            authority=authority,
        )
        super().register_witness(witness)
        self._verify_provider_witness(witness)
        return witness

    def _verify_provider_witness(
        self,
        witness: _legacy.AvailabilityWitnessAuthority,
    ) -> None:
        if not witness.witness_kind.startswith("provider-"):
            return
        if witness.witness_kind != HISTORICAL_CAPTURE_WITNESS_KIND:
            raise _legacy.SourceRevisionAuthorityError(
                "provider witness kind has no canonical capture resolver"
            )
        authority = self._resolve_capture_bundle(
            witness.availability_witness_id
        )
        expected = self._expected_provider_witness(
            availability_witness_id=witness.availability_witness_id,
            authority=authority,
            recorded_at=witness.recorded_at,
        )
        if expected.authority_sha256 != witness.authority_sha256:
            raise _legacy.SourceRevisionAuthorityError(
                "provider availability witness does not match canonical capture authority"
            )

    def _verify_provider_state(self) -> None:
        state = self._read()
        for raw in state["policies"]:
            policy = _legacy.RevisionPolicyAuthority.from_payload(raw)
            self._verify_provider_policy(policy)
        for raw in state["witnesses"]:
            witness = _legacy.AvailabilityWitnessAuthority.from_payload(raw)
            self._verify_provider_witness(witness)

    def resolve_witness(
        self,
        availability_witness_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> _legacy.AvailabilityWitnessAuthority:
        witness = super().resolve_witness(
            availability_witness_id,
            expected_sha256=expected_sha256,
        )
        self._verify_provider_witness(witness)
        return witness

    def register_revision(
        self,
        revision: _legacy.SourceRevisionAuthority,
    ) -> str:
        if isinstance(revision, _legacy.SourceRevisionAuthority):
            self.resolve_witness(
                revision.availability_witness_id,
                expected_sha256=revision.availability_witness_record_sha256,
            )
            self.resolve_policy(
                revision.revision_policy_id,
                expected_sha256=revision.revision_policy_record_sha256,
            )
        return super().register_revision(revision)

    def resolve_revision(
        self,
        source_revision_authority_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> _legacy.SourceRevisionAuthority:
        revision = super().resolve_revision(
            source_revision_authority_id,
            expected_sha256=expected_sha256,
        )
        self.resolve_witness(
            revision.availability_witness_id,
            expected_sha256=revision.availability_witness_record_sha256,
        )
        self.resolve_policy(
            revision.revision_policy_id,
            expected_sha256=revision.revision_policy_record_sha256,
        )
        return revision


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
