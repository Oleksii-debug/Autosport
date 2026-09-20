from __future__ import annotations

"""Point-in-time authority with provider provenance and external rollback fencing.

The accumulated EXTVAL implementation remains intact in the private legacy
module.  This compatibility surface binds provider availability to canonical
persisted capture evidence and binds holdout-consumption freshness to Autosport's
shared machine-state MonotonicWorkspaceAuthority.  The semantic ledger remains
workspace-local; only its opaque freshness/digest proof lives outside that
rollback domain.
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
    HistoricalSnapshotCapture,
    assert_historical_snapshot_capture_authoritative,
    resolve_historical_snapshot_authority,
)
from .integrity import atomic_write_json
from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
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
        capture: HistoricalSnapshotCapture,
        recorded_at: datetime,
    ) -> _legacy.AvailabilityWitnessAuthority:
        try:
            assert_historical_snapshot_capture_authoritative(capture)
        except (TypeError, ValueError) as exc:
            raise _legacy.SourceRevisionAuthorityError(
                "historical provider capture was not issued by canonical capture path"
            ) from exc

        market_path = Path(capture.output_path)
        evidence_path = Path(capture.evidence_path)
        try:
            authority = resolve_historical_snapshot_authority(
                market_path=market_path,
                evidence_path=evidence_path,
            )
        except (OSError, ValueError) as exc:
            raise _legacy.SourceRevisionAuthorityError(
                "canonical provider capture evidence cannot be resolved"
            ) from exc
        if (
            authority.source_revision_sha256 != capture.response_sha256
            or authority.source_as_of != capture.snapshot_at
            or authority.available_at != capture.captured_at
            or hashlib.sha256(market_path.read_bytes()).hexdigest()
            != capture.market_sha256
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "canonical provider capture files changed after issuance"
            )

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
            # Validate the referenced immutable authority digests before any
            # mutation, then re-resolve provider-backed provenance.
            self.resolve_policy(
                revision.revision_policy_id,
                expected_sha256=revision.revision_policy_record_sha256,
            )
            self.resolve_witness(
                revision.availability_witness_id,
                expected_sha256=revision.availability_witness_record_sha256,
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
    """Holdout ledger fenced by the shared workspace-external monotonic authority."""

    AUTHORITY_DOMAIN: Final = "data.point-in-time.holdout-consumption"
    AUTHORITY_KEY: Final = "holdout-consumption-ledger-v1"

    def __init__(
        self,
        workspace: str | Path,
        *,
        scientific_registry: _legacy.ScientificRegistry,
        monotonic_authority_root: str | Path | None = None,
    ) -> None:
        super().__init__(workspace, scientific_registry=scientific_registry)
        try:
            self.monotonic_authority = MonotonicWorkspaceAuthority(
                workspace=self.workspace.resolve(strict=False),
                domain=self.AUTHORITY_DOMAIN,
                key=self.AUTHORITY_KEY,
                authority_root=monotonic_authority_root,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise _legacy.HoldoutConsumptionError(
                "cannot initialize independent holdout monotonic authority"
            ) from exc

    @staticmethod
    def _authority_state_sha256(state: _legacy._LedgerState) -> str | None:
        if state.generation == 0:
            return None
        return state.ledger_sha256

    def _authority_history_unlocked(self):
        try:
            return self.monotonic_authority.read_history()
        except MonotonicWorkspaceAuthorityError as exc:
            raise _legacy.HoldoutConsumptionError(
                "cannot read independent holdout monotonic authority"
            ) from exc

    def _recovery_transaction_unlocked(
        self,
        state: _legacy._LedgerState,
    ) -> tuple[str | None, str | None]:
        if not state.receipts:
            return None, None
        binding = state.receipts[-1].receipt_sha256
        history = self._authority_history_unlocked()
        if not history:
            return None, binding
        latest = history[-1]
        if (
            latest.phase.value == "PREPARE"
            and latest.intended_state_sha256 == self._authority_state_sha256(state)
            and latest.semantic_binding_sha256 == binding
        ):
            return latest.tx_id, binding
        return None, binding

    def _new_transaction_id_unlocked(self, receipt_sha256: str) -> str:
        # Authority transaction identity is an attempt identity, not the semantic
        # receipt identity.  A PREPARE that was durably ABORTed must never be
        # reused on an exact semantic retry, because shared authority correctly
        # refuses to COMMIT a terminal ABORT.  The append-only history length is
        # stable under the enclosing workspace writer lock and gives each retry a
        # fresh deterministic attempt id without weakening receipt idempotency.
        next_record = len(self._authority_history_unlocked()) + 1
        return f"holdout-consumption:{next_record}:{receipt_sha256}"

    def _recover_authority_unlocked(
        self,
        state: _legacy._LedgerState,
    ) -> None:
        tx_id, binding = self._recovery_transaction_unlocked(state)
        try:
            self.monotonic_authority.recover(
                observed_state_sha256=self._authority_state_sha256(state),
                tx_id=tx_id,
                semantic_binding_sha256=binding,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise _legacy.HoldoutConsumptionError(
                "holdout ledger rejected by independent monotonic authority"
            ) from exc

    def _load_with_authority_unlocked(self) -> _legacy._LedgerState:
        # Preserve all legacy ledger/anchor integrity checks before consulting the
        # external freshness root.  This keeps malformed/half-published local
        # state fail-closed rather than asking the generic authority to guess
        # domain semantics.
        state = super()._load_unlocked()
        self._recover_authority_unlocked(state)
        return state

    def receipts(self) -> tuple[_legacy.HoldoutConsumptionReceipt, ...]:
        with WorkspaceEconomicLock(self.workspace):
            return self._load_with_authority_unlocked().receipts

    def receipt_for(
        self,
        holdout_access_id: str,
    ) -> _legacy.HoldoutConsumptionReceipt | None:
        target = _legacy._sha256(holdout_access_id, "holdout_access_id")
        with WorkspaceEconomicLock(self.workspace):
            for receipt in self._load_with_authority_unlocked().receipts:
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
        """Consume once with PREPARE -> local publish -> exact COMMIT fencing."""

        _legacy._require_registered_dataset(self.scientific_registry, dataset_snapshot)
        _legacy._validate_holdout_available(dataset_snapshot, consumed_at=consumed_at)
        access_id = _legacy.holdout_identity(
            scientific_registry=self.scientific_registry,
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=research_protocol_id,
            confirmation_trial_family_id=confirmation_trial_family_id,
        )

        # Global order is protected workspace lock -> shared authority lock, as
        # required by MonotonicWorkspaceAuthority.  The authority stores only
        # opaque ledger digests/bindings; this ledger remains the semantic truth.
        with WorkspaceEconomicLock(self.workspace):
            current = self._load_with_authority_unlocked()
            if any(
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
            tx_id = self._new_transaction_id_unlocked(receipt.receipt_sha256)
            binding = receipt.receipt_sha256

            try:
                self.monotonic_authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=self._authority_state_sha256(current),
                    intended_state_sha256=next_state.ledger_sha256,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise _legacy.HoldoutConsumptionError(
                    "holdout consumption could not reserve monotonic authority"
                ) from exc

            atomic_write_json(self.path, next_payload)
            atomic_write_json(self.anchor_path, self._anchor_payload(next_state))
            verified_local = super()._load_unlocked()
            if verified_local != next_state:
                raise _legacy.HoldoutConsumptionError(
                    "published holdout ledger did not verify after write"
                )

            try:
                self.monotonic_authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=next_state.ledger_sha256,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                # The complete local pair is intentionally left intact.  On
                # restart, recover() identifies the exact pending attempt from
                # the external history and can commit only its matching digest
                # and receipt binding.
                raise _legacy.HoldoutConsumptionError(
                    "holdout ledger published but monotonic commit was not completed"
                ) from exc

            verified = self._load_with_authority_unlocked()
            if verified != next_state:
                raise _legacy.HoldoutConsumptionError(
                    "committed holdout ledger did not verify against monotonic authority"
                )
            return receipt


__all__ = list(_legacy.__all__)
