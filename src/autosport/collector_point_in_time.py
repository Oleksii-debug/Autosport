from __future__ import annotations

"""Canonical bridge from durable collector application evidence into point-in-time authority.

This module deliberately reuses the existing point-in-time source authority store.
It does not create a second collector, checkpoint ledger, or availability registry.
Collector-backed witnesses are minted only by re-resolving an immutable CollectorDelta
and its durable DesktopDeltaCheckpointStore application receipt.
"""

from datetime import datetime
from typing import Final

from . import _point_in_time_authority_legacy as _legacy
from .causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
)
from .point_in_time_authority import SourceRevisionAuthorityStore


COLLECTOR_APPLICATION_WITNESS_KIND: Final = "collector-desktop-application-v1"
_COLLECTOR_SOURCE_REVISION_PREFIX: Final = "collector-delta:"


class CollectorPointInTimeSourceRevisionAuthorityStore(SourceRevisionAuthorityStore):
    """Source authority store that mechanically re-resolves live collector evidence."""

    def __init__(
        self,
        workspace,
        *,
        collector_store: CollectorDeltaStore,
        checkpoint_store: DesktopDeltaCheckpointStore,
    ) -> None:
        if not isinstance(collector_store, CollectorDeltaStore):
            raise TypeError("collector_store must be a CollectorDeltaStore")
        if not isinstance(checkpoint_store, DesktopDeltaCheckpointStore):
            raise TypeError("checkpoint_store must be a DesktopDeltaCheckpointStore")
        self.collector_store = collector_store
        self.checkpoint_store = checkpoint_store
        super().__init__(workspace)
        self._verify_collector_state()

    @classmethod
    def initialize_pristine(
        cls,
        workspace,
        *,
        collector_store: CollectorDeltaStore,
        checkpoint_store: DesktopDeltaCheckpointStore,
    ) -> "CollectorPointInTimeSourceRevisionAuthorityStore":
        SourceRevisionAuthorityStore.initialize_pristine(workspace)
        return cls(
            workspace,
            collector_store=collector_store,
            checkpoint_store=checkpoint_store,
        )

    @staticmethod
    def _collector_policy(
        *,
        source_identity: str,
        revision_policy_id: str,
        frozen_at: datetime,
    ) -> _legacy.RevisionPolicyAuthority:
        source = _legacy._text(source_identity, "source_identity")
        payload = {
            "schema": "autosport.revision_availability_policy",
            "schema_version": 1,
            "source_identity": source,
            "policy_version": "1",
            "witness_kind": COLLECTOR_APPLICATION_WITNESS_KIND,
            "availability_semantics": "source_as_of<=available_at",
        }
        return _legacy.RevisionPolicyAuthority.create(
            revision_policy_id=revision_policy_id,
            policy_content_json=_legacy._canonical_json(payload),
            frozen_at=frozen_at,
        )

    def _verify_collector_policy(
        self,
        policy: _legacy.RevisionPolicyAuthority,
    ) -> None:
        if policy.witness_kind != COLLECTOR_APPLICATION_WITNESS_KIND:
            return
        expected = self._collector_policy(
            source_identity=policy.source_identity,
            revision_policy_id=policy.revision_policy_id,
            frozen_at=policy.frozen_at,
        )
        if expected.authority_sha256 != policy.authority_sha256:
            raise _legacy.SourceRevisionAuthorityError(
                "collector revision policy is not canonical"
            )

    def register_policy(
        self,
        policy: _legacy.RevisionPolicyAuthority,
    ) -> str:
        if (
            isinstance(policy, _legacy.RevisionPolicyAuthority)
            and policy.witness_kind == COLLECTOR_APPLICATION_WITNESS_KIND
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "collector revision policy must be registered from canonical collector authority"
            )
        return super().register_policy(policy)

    def register_collector_policy(
        self,
        *,
        source_identity: str,
        revision_policy_id: str,
        frozen_at: datetime,
    ) -> _legacy.RevisionPolicyAuthority:
        policy = self._collector_policy(
            source_identity=source_identity,
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
        self._verify_collector_policy(policy)
        return policy

    @staticmethod
    def _source_revision(delta: CollectorDelta) -> str:
        return f"{_COLLECTOR_SOURCE_REVISION_PREFIX}{delta.delta_id}"

    @staticmethod
    def _collector_binding_payload(
        delta: CollectorDelta,
        receipt: DesktopApplicationReceipt,
    ) -> dict[str, object]:
        delta.validate()
        receipt.validate()
        if receipt.delta_id != delta.delta_id:
            raise _legacy.SourceRevisionAuthorityError(
                "collector application receipt belongs to another delta"
            )
        if receipt.canonical_event_digest != delta.canonical_event_digest:
            raise _legacy.SourceRevisionAuthorityError(
                "collector application receipt digest does not match delta"
            )
        desktop_available = _legacy._instant(
            delta.desktop_available_at,
            "desktop_available_at",
        )
        applied_at = _legacy._instant(receipt.applied_at, "applied_at")
        if applied_at < desktop_available:
            raise _legacy.SourceRevisionAuthorityError(
                "collector application receipt predates desktop availability"
            )
        return {
            "schema": "autosport.collector_point_in_time_binding",
            "schema_version": 1,
            "delta_id": delta.delta_id,
            "source_id": delta.source_id,
            "lawful_terms_ref": delta.lawful_terms_ref,
            "retention_ref": delta.retention_ref,
            "stream_epoch": delta.stream_epoch,
            "source_cursor": delta.source_cursor,
            "cursor_position": delta.cursor_position,
            "event_dedupe_key": delta.event_dedupe_key,
            "event_id": delta.event_id,
            "source_payload_digest": delta.source_payload_digest,
            "canonical_event_digest": delta.canonical_event_digest,
            "source_observed_at": delta.source_observed_at,
            "collector_received_at": delta.collector_received_at,
            "collector_committed_at": delta.collector_committed_at,
            "desktop_available_at": delta.desktop_available_at,
            "revision_of": delta.revision_of,
            "revision_number": delta.revision_number,
            "application_receipt_id": receipt.receipt_id,
            "application_applied_at": receipt.applied_at,
        }

    @classmethod
    def _collector_binding_sha256(
        cls,
        delta: CollectorDelta,
        receipt: DesktopApplicationReceipt,
    ) -> str:
        return _legacy._digest(cls._collector_binding_payload(delta, receipt))

    def _resolve_collector_application(
        self,
        delta_id: str,
    ) -> tuple[CollectorDelta, DesktopApplicationReceipt]:
        wanted = _legacy._text(delta_id, "delta_id")
        try:
            delta = self.collector_store.get(wanted)
        except (OSError, TypeError, ValueError) as exc:
            raise _legacy.SourceRevisionAuthorityError(
                "canonical collector delta cannot be resolved"
            ) from exc
        if delta is None:
            raise _legacy.SourceRevisionAuthorityError(
                "canonical collector delta is missing"
            )
        try:
            receipt = self.checkpoint_store.application_receipt(delta)
        except (OSError, TypeError, ValueError) as exc:
            raise _legacy.SourceRevisionAuthorityError(
                "canonical desktop application receipt cannot be resolved"
            ) from exc
        if receipt is None:
            raise _legacy.SourceRevisionAuthorityError(
                "collector delta has no completed durable desktop application receipt"
            )
        self._collector_binding_payload(delta, receipt)
        return delta, receipt

    @classmethod
    def _expected_collector_witness(
        cls,
        *,
        availability_witness_id: str,
        delta: CollectorDelta,
        receipt: DesktopApplicationReceipt,
        recorded_at: datetime,
    ) -> _legacy.AvailabilityWitnessAuthority:
        binding_sha256 = cls._collector_binding_sha256(delta, receipt)
        payload = {
            "schema": "autosport.source_availability_witness",
            "schema_version": 1,
            "source_identity": delta.source_id,
            "source_revision": cls._source_revision(delta),
            "source_revision_sha256": binding_sha256,
            "witness_kind": COLLECTOR_APPLICATION_WITNESS_KIND,
            "source_as_of": delta.source_observed_at,
            "available_at": receipt.applied_at,
        }
        return _legacy.AvailabilityWitnessAuthority.create(
            availability_witness_id=availability_witness_id,
            witness_content_json=_legacy._canonical_json(payload),
            recorded_at=recorded_at,
        )

    @classmethod
    def _collector_witness_id(
        cls,
        delta: CollectorDelta,
        receipt: DesktopApplicationReceipt,
    ) -> str:
        digest = cls._collector_binding_sha256(delta, receipt)
        return f"collector-application:{digest}"

    def _verify_collector_witness(
        self,
        witness: _legacy.AvailabilityWitnessAuthority,
    ) -> None:
        if witness.witness_kind != COLLECTOR_APPLICATION_WITNESS_KIND:
            return
        if not witness.source_revision.startswith(_COLLECTOR_SOURCE_REVISION_PREFIX):
            raise _legacy.SourceRevisionAuthorityError(
                "collector availability witness has invalid source revision identity"
            )
        delta_id = witness.source_revision[len(_COLLECTOR_SOURCE_REVISION_PREFIX) :]
        if not delta_id:
            raise _legacy.SourceRevisionAuthorityError(
                "collector availability witness has empty delta identity"
            )
        delta, receipt = self._resolve_collector_application(delta_id)
        expected_id = self._collector_witness_id(delta, receipt)
        if witness.availability_witness_id != expected_id:
            raise _legacy.SourceRevisionAuthorityError(
                "collector availability witness identity does not match durable application evidence"
            )
        expected = self._expected_collector_witness(
            availability_witness_id=expected_id,
            delta=delta,
            receipt=receipt,
            recorded_at=witness.recorded_at,
        )
        if expected.authority_sha256 != witness.authority_sha256:
            raise _legacy.SourceRevisionAuthorityError(
                "collector availability witness does not match durable application evidence"
            )

    def register_witness(
        self,
        witness: _legacy.AvailabilityWitnessAuthority,
    ) -> str:
        if (
            isinstance(witness, _legacy.AvailabilityWitnessAuthority)
            and witness.witness_kind == COLLECTOR_APPLICATION_WITNESS_KIND
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "collector availability witness requires canonical collector/checkpoint evidence"
            )
        return super().register_witness(witness)

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
        self._verify_collector_witness(witness)
        return witness

    @staticmethod
    def _source_revision_authority_id(
        *,
        delta: CollectorDelta,
        policy: _legacy.RevisionPolicyAuthority,
        witness: _legacy.AvailabilityWitnessAuthority,
    ) -> str:
        payload = {
            "schema": "autosport.collector_source_revision_authority_identity",
            "schema_version": 1,
            "source_revision": CollectorPointInTimeSourceRevisionAuthorityStore._source_revision(
                delta
            ),
            "source_revision_sha256": witness.source_revision_sha256,
            "revision_policy_record_sha256": policy.authority_sha256,
            "availability_witness_record_sha256": witness.authority_sha256,
        }
        return f"collector-source-authority:{_legacy._digest(payload)}"

    def register_collector_revision(
        self,
        *,
        delta_id: str,
        revision_policy_id: str,
        recorded_at: datetime,
    ) -> _legacy.SourceRevisionAuthority:
        delta, receipt = self._resolve_collector_application(delta_id)
        policy = self.resolve_policy(revision_policy_id)
        if (
            policy.source_identity != delta.source_id
            or policy.witness_kind != COLLECTOR_APPLICATION_WITNESS_KIND
        ):
            raise _legacy.SourceRevisionAuthorityError(
                "collector revision policy does not authorize this source/delta"
            )
        witness_id = self._collector_witness_id(delta, receipt)
        witness = self._expected_collector_witness(
            availability_witness_id=witness_id,
            delta=delta,
            receipt=receipt,
            recorded_at=recorded_at,
        )
        super().register_witness(witness)
        self._verify_collector_witness(witness)
        revision = _legacy.SourceRevisionAuthority(
            source_revision_authority_id=self._source_revision_authority_id(
                delta=delta,
                policy=policy,
                witness=witness,
            ),
            source_identity=witness.source_identity,
            source_revision=witness.source_revision,
            source_revision_sha256=witness.source_revision_sha256,
            revision_policy_id=policy.revision_policy_id,
            revision_policy_record_sha256=policy.authority_sha256,
            availability_witness_id=witness.availability_witness_id,
            availability_witness_sha256=witness.witness_content_sha256,
            availability_witness_record_sha256=witness.authority_sha256,
            witness_kind=witness.witness_kind,
            source_as_of=witness.source_as_of,
            available_at=witness.available_at,
            recorded_at=recorded_at,
        )
        super().register_revision(revision)
        return self.resolve_revision(
            revision.source_revision_authority_id,
            expected_sha256=revision.authority_sha256,
        )

    def _verify_collector_state(self) -> None:
        state = self._read()
        for raw in state["policies"]:
            policy = _legacy.RevisionPolicyAuthority.from_payload(raw)
            self._verify_collector_policy(policy)
        for raw in state["witnesses"]:
            witness = _legacy.AvailabilityWitnessAuthority.from_payload(raw)
            self._verify_collector_witness(witness)

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
        if revision.witness_kind == COLLECTOR_APPLICATION_WITNESS_KIND:
            witness = self.resolve_witness(
                revision.availability_witness_id,
                expected_sha256=revision.availability_witness_record_sha256,
            )
            if (
                witness.source_revision != revision.source_revision
                or witness.source_revision_sha256 != revision.source_revision_sha256
                or witness.source_as_of != revision.source_as_of
                or witness.available_at != revision.available_at
            ):
                raise _legacy.SourceRevisionAuthorityError(
                    "collector source revision no longer matches durable application evidence"
                )
        return revision
