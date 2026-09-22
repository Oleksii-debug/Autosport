"""Source-owned first-publication authority for point-in-time feature artifacts.

Positive point-in-time evidence needs two independent facts: exact immutable
DatasetSnapshot/FeatureSet/payload provenance, and proof that the source side had
actually materialized those bytes by the decision cutoff.  This module owns the
second fact.

The publication ledger is compositionally bound to the exact canonical
``DatasetSnapshotLineageAuthority`` workspace/instance/root.  Its local state is
protected by ``MonotonicWorkspaceAuthority`` with PREPARE -> durable publish ->
COMMIT, so restoring a valid-old ledger or deleting it cannot silently erase a
later first-publication fact while the independent machine authority survives.

Low-level publication is deliberately not a public evaluator operation.  A writer
capability is issued only by the canonical ``HeadlessCollectorService`` source
runtime.  The resulting ``SourceFeatureArtifactMaterializer`` re-resolves the exact
collector delta, registry records, lineage proof, and bytes-derived
``FeatureArtifactProvenance`` before asking the authority to stamp NOW.  Ordinary
callers may construct the resolver-facing object, but cannot exercise publication
without that source-runtime capability.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping

from . import _point_in_time_authority_runtime_repair as runtime_repair
from . import _point_in_time_feature_provenance_guard as provenance_guard
from . import point_in_time_evidence as evidence
from .causal_collector import CollectorDelta, CollectorDeltaStore
from .dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from .integrity import atomic_write_json
from .monotonic_workspace_authority import (
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .scientific_registry import DatasetSnapshot, FeatureSet, RegistryEntry
from .workspace_lock import WorkspaceEconomicLock

_SCHEMA: Final = "autosport.source-feature-artifact-authority"
_SCHEMA_VERSION: Final = 2
_CANONICAL_FILENAME: Final = "source-feature-artifact-publications.json"
_MONOTONIC_DOMAIN: Final = "source-feature-artifact-publication"
_MONOTONIC_KEY: Final = "first-source-materialization-v2"
_MONOTONIC_BINDING_KIND: Final = "autosport-source-feature-artifact-state-v2"
_HEX = frozenset("0123456789abcdef")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise evidence.PointInTimeEvidenceError(
            f"{name} must be a non-empty canonical string"
        )
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise evidence.PointInTimeEvidenceError(
            f"{name} must be a canonical SHA-256 hex string"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise evidence.PointInTimeEvidenceError(
            f"{name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise evidence.PointInTimeEvidenceError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


COLLECTOR_SOURCE_FEATURE_PRODUCER_ID: Final = (
    "autosport.collector-source-feature-producer-v1"
)
_COLLECTOR_SOURCE_FEATURE_DEFINITION: Final[dict[str, Any]] = {
    "kind": "autosport.collector-source-feature-v1",
    "schema_version": 1,
    "input_authority": "CollectorDeltaStore",
    "input_identity": "exact durable CollectorDelta by delta_id",
    "projection": "canonical collector delta plus exact dataset/FeatureSet identity",
}
COLLECTOR_SOURCE_FEATURE_DEFINITION_SHA256: Final = _digest(
    _COLLECTOR_SOURCE_FEATURE_DEFINITION
)
COLLECTOR_SOURCE_FEATURE_PRODUCER_SHA256: Final = hashlib.sha256(
    (
        COLLECTOR_SOURCE_FEATURE_PRODUCER_ID
        + ":"
        + COLLECTOR_SOURCE_FEATURE_DEFINITION_SHA256
    ).encode("utf-8")
).hexdigest()
_CANONICAL_COLLECTOR_FILENAME: Final = "collector_deltas.json"
_HEADLESS_COLLECTOR_ISSUANCE_CAPABILITY: Final[object] = object()
_SOURCE_INGESTION_RECEIPT_DOMAIN: Final = "source-feature-ingestion-receipt-v1"
_SOURCE_INGESTION_RECEIPT_KEY_PREFIX: Final = "collector-delta-v1:"
_SOURCE_INGESTION_RECEIPT_BINDING_KIND: Final = (
    "autosport-source-feature-ingestion-receipt-v1"
)


def _require_canonical_collector_store(
    lineage_authority: DatasetSnapshotLineageAuthority,
    collector_store: CollectorDeltaStore,
) -> CollectorDeltaStore:
    from . import causal_collector as collector_module

    canonical_type = collector_module.CollectorDeltaStore
    if type(collector_store) is not canonical_type:
        raise evidence.PointInTimeEvidenceError(
            "source feature materialization requires the exact canonical CollectorDeltaStore"
        )
    expected_path = lineage_authority.path.with_name(
        _CANONICAL_COLLECTOR_FILENAME
    ).resolve(strict=False)
    try:
        observed_path = collector_store.path.resolve(strict=False)
    except (AttributeError, OSError) as exc:
        raise evidence.PointInTimeEvidenceError(
            "canonical collector source path is unavailable"
        ) from exc
    if observed_path != expected_path:
        raise evidence.PointInTimeEvidenceError(
            "source feature materialization must use the canonical collector store "
            "from the exact lineage workspace"
        )
    return collector_store


def _source_ingestion_receipt_state_sha256(delta: CollectorDelta) -> str:
    if type(delta) is not CollectorDelta:
        raise evidence.PointInTimeEvidenceError(
            "source ingestion receipt requires an exact CollectorDelta"
        )
    delta.validate()
    return _digest(
        {
            "kind": "autosport.source-feature-ingestion-state-v1",
            "delta": delta.to_dict(),
        }
    )


def _source_ingestion_receipt_binding_sha256(
    delta: CollectorDelta,
    state_sha256: str,
) -> str:
    return _digest(
        {
            "kind": _SOURCE_INGESTION_RECEIPT_BINDING_KIND,
            "delta_id": delta.delta_id,
            "source_id": delta.source_id,
            "stream_epoch": delta.stream_epoch,
            "state_sha256": _sha256(state_sha256, "state_sha256"),
        }
    )


def _source_ingestion_receipt_authority(
    collector_store: CollectorDeltaStore,
    delta: CollectorDelta,
) -> MonotonicWorkspaceAuthority:
    from . import causal_collector as collector_module

    if type(collector_store) is not collector_module.CollectorDeltaStore:
        raise evidence.PointInTimeEvidenceError(
            "source ingestion receipt requires the exact canonical CollectorDeltaStore"
        )
    if collector_store.path.name != _CANONICAL_COLLECTOR_FILENAME:
        raise evidence.PointInTimeEvidenceError(
            "source ingestion receipt requires the canonical collector store filename"
        )
    delta.validate()
    delta_key = hashlib.sha256(delta.delta_id.encode("utf-8")).hexdigest()
    return MonotonicWorkspaceAuthority(
        workspace=collector_store.path.parent.resolve(strict=False),
        domain=_SOURCE_INGESTION_RECEIPT_DOMAIN,
        key=f"{_SOURCE_INGESTION_RECEIPT_KEY_PREFIX}{delta_key}",
    )


def _require_source_ingestion_receipt(
    collector_store: CollectorDeltaStore,
    delta: CollectorDelta,
) -> None:
    persisted = type(collector_store).get(collector_store, delta.delta_id)
    if persisted is None or persisted.to_dict() != delta.to_dict():
        raise evidence.PointInTimeEvidenceError(
            "source ingestion receipt cannot re-resolve the exact canonical collector delta"
        )
    state_sha256 = _source_ingestion_receipt_state_sha256(delta)
    binding_sha256 = _source_ingestion_receipt_binding_sha256(
        delta, state_sha256
    )
    authority = _source_ingestion_receipt_authority(collector_store, delta)
    try:
        recovery = authority.recover(observed_state_sha256=state_sha256)
    except MonotonicAuthorityRecoveryRequiredError:
        history = authority.read_history()
        if not history:
            raise evidence.PointInTimeEvidenceError(
                "source ingestion receipt recovery history disappeared"
            )
        pending = history[-1]
        if (
            pending.intended_state_sha256 != state_sha256
            or pending.semantic_binding_sha256 != binding_sha256
        ):
            raise evidence.PointInTimeEvidenceError(
                "source ingestion receipt pending state does not match canonical collector delta"
            )
        try:
            recovery = authority.recover(
                observed_state_sha256=state_sha256,
                tx_id=pending.tx_id,
                semantic_binding_sha256=binding_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise evidence.PointInTimeEvidenceError(
                "source ingestion receipt recovery failed closed"
            ) from exc
    except MonotonicWorkspaceAuthorityError as exc:
        raise evidence.PointInTimeEvidenceError(
            "canonical collector delta lacks a product-owned source ingestion receipt"
        ) from exc
    if (
        recovery.committed_generation < 1
        or recovery.committed_state_sha256 != state_sha256
    ):
        raise evidence.PointInTimeEvidenceError(
            "canonical collector delta lacks a committed source ingestion receipt"
        )


def _append_with_source_ingestion_receipt(
    *,
    service: object,
    delta: CollectorDelta,
    original_append,
) -> bool:
    """Wrap the real collector admission with a durable per-delta source receipt.

    PREPARE exists before the canonical service commit.  Only a normal
    changed=True service commit may COMMIT the receipt.  A hard crash after SQLite
    commit leaves PREPARE durable; later resolution may self-COMMIT only when the
    exact same immutable CollectorDelta is present.  A delta that already existed
    before this source-service call can never be retroactively receipted here.
    """

    from .collector_service import HeadlessCollectorService

    if type(service) is not HeadlessCollectorService:
        raise evidence.PointInTimeEvidenceError(
            "source ingestion receipt requires exact HeadlessCollectorService"
        )
    if type(delta) is not CollectorDelta:
        raise evidence.PointInTimeEvidenceError(
            "source ingestion receipt requires exact CollectorDelta"
        )
    delta.validate()
    source = service._require_source_identity(
        expected_stream_epoch=delta.stream_epoch
    )
    if delta.source_id != service.source_id or delta.source_id != source.source_id:
        raise evidence.PointInTimeEvidenceError(
            "source ingestion receipt delta is outside collector service source authority"
        )
    collector_store = service.delta_store
    authority = _source_ingestion_receipt_authority(collector_store, delta)
    existing = type(collector_store).get(collector_store, delta.delta_id)
    if existing is not None:
        # Existing bytes predate this call.  The canonical collector may still
        # process the duplicate/epoch evidence, but this call is not allowed to
        # mint source-origin authority for those bytes.
        return original_append(service, delta)

    state_sha256 = _source_ingestion_receipt_state_sha256(delta)
    binding_sha256 = _source_ingestion_receipt_binding_sha256(
        delta, state_sha256
    )
    try:
        authority.recover(observed_state_sha256=None)
    except MonotonicWorkspaceAuthorityError as exc:
        raise evidence.PointInTimeEvidenceError(
            "source ingestion receipt history rejects a new collector admission"
        ) from exc

    tx_id = f"source-ingestion-{uuid.uuid4().hex}"
    try:
        authority.prepare(
            tx_id=tx_id,
            observed_state_sha256=None,
            intended_state_sha256=state_sha256,
            semantic_binding_sha256=binding_sha256,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise evidence.PointInTimeEvidenceError(
            "source ingestion receipt PREPARE failed closed"
        ) from exc

    try:
        changed = original_append(service, delta)
    except BaseException as primary:
        try:
            authority.abort(
                tx_id=tx_id,
                observed_state_sha256=None,
                semantic_binding_sha256=binding_sha256,
            )
        except BaseException as cleanup:
            try:
                primary.add_note(
                    "source ingestion receipt ABORT also failed after collector "
                    f"admission error: {type(cleanup).__name__}: {cleanup}"
                )
            except BaseException:
                pass
        raise

    if changed is not True:
        try:
            authority.abort(
                tx_id=tx_id,
                observed_state_sha256=None,
                semantic_binding_sha256=binding_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise evidence.PointInTimeEvidenceError(
                "duplicate collector admission could not abort source receipt"
            ) from exc
        return False

    persisted = type(collector_store).get(collector_store, delta.delta_id)
    if persisted is None or persisted.to_dict() != delta.to_dict():
        raise evidence.PointInTimeEvidenceError(
            "collector reported a new delta but exact durable bytes cannot be re-resolved"
        )
    try:
        authority.commit(
            tx_id=tx_id,
            observed_state_sha256=state_sha256,
            semantic_binding_sha256=binding_sha256,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        # Do not ABORT here.  The exact delta is already durable and PREPARE is the
        # crash-recovery proof; a later reader can safely self-COMMIT it.
        raise evidence.PointInTimeEvidenceError(
            "collector delta committed but source ingestion receipt COMMIT is pending"
        ) from exc
    return True


def collector_source_feature_payload(
    *,
    source_delta: CollectorDelta,
    dataset_snapshot: DatasetSnapshot,
    feature_set: FeatureSet,
) -> bytes:
    """Build the supported source-owned feature artifact from durable input.

    This deterministic encoder is read-only, not an issuance capability. The
    writer re-resolves source_delta from the canonical CollectorDeltaStore before
    calling it; callers cannot provide arbitrary feature bytes to the writer.
    """

    if type(source_delta) is not CollectorDelta:
        raise evidence.PointInTimeEvidenceError(
            "source_delta must be an exact CollectorDelta"
        )
    if type(dataset_snapshot) is not DatasetSnapshot:
        raise evidence.PointInTimeEvidenceError(
            "dataset_snapshot must be an exact DatasetSnapshot"
        )
    if type(feature_set) is not FeatureSet:
        raise evidence.PointInTimeEvidenceError(
            "feature_set must be an exact FeatureSet"
        )
    source_delta.validate()
    if source_delta.source_id != dataset_snapshot.source_identity:
        raise evidence.PointInTimeEvidenceError(
            "collector source identity does not match DatasetSnapshot"
        )
    if _instant(source_delta.collector_committed_at, "collector_committed_at") > _instant(
        dataset_snapshot.causal_cutoff, "dataset_snapshot.causal_cutoff"
    ):
        raise evidence.PointInTimeEvidenceError(
            "collector source commit is after DatasetSnapshot causal cutoff"
        )
    if _instant(source_delta.desktop_available_at, "desktop_available_at") > _instant(
        dataset_snapshot.available_at_utc, "dataset_snapshot.available_at_utc"
    ):
        raise evidence.PointInTimeEvidenceError(
            "collector source was not product-available by DatasetSnapshot availability"
        )
    if (
        feature_set.definition_sha256.lower()
        != COLLECTOR_SOURCE_FEATURE_DEFINITION_SHA256
        or feature_set.source_sha256.lower()
        != COLLECTOR_SOURCE_FEATURE_PRODUCER_SHA256
    ):
        raise evidence.PointInTimeEvidenceError(
            "FeatureSet is not bound to the canonical collector source-feature producer"
        )

    payload = {
        "kind": "autosport.collector-source-feature-artifact-v1",
        "schema_version": 1,
        "producer_id": COLLECTOR_SOURCE_FEATURE_PRODUCER_ID,
        "producer_contract_sha256": COLLECTOR_SOURCE_FEATURE_PRODUCER_SHA256,
        "dataset_snapshot_id": dataset_snapshot.dataset_snapshot_id,
        "dataset_source_identity": dataset_snapshot.source_identity,
        "feature_set_id": feature_set.feature_set_id,
        "feature_version": feature_set.version,
        "feature_definition_sha256": feature_set.definition_sha256.lower(),
        "feature_source_sha256": feature_set.source_sha256.lower(),
        "source_delta": source_delta.to_dict(),
    }
    return (_canonical_json(payload) + "\n").encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise evidence.PointInTimeEvidenceError(
                f"duplicate feature publication state key: {key}"
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise evidence.PointInTimeEvidenceError(
        f"non-finite feature publication state value: {value}"
    )


def _publication_key(dataset_snapshot_id: str, feature_set_id: str) -> str:
    return _digest(
        {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "dataset_snapshot_id": _text(
                dataset_snapshot_id, "dataset_snapshot_id"
            ),
            "feature_set_id": _text(feature_set_id, "feature_set_id"),
        }
    )


def _registry_entry(
    lineage: DatasetSnapshotLineageAuthority,
    *,
    record_type: str,
    record_id: str,
    exact_payload: Mapping[str, Any],
) -> RegistryEntry:
    entry = lineage.registry.get(record_type, record_id)
    if entry is None:
        raise evidence.PointInTimeEvidenceError(
            f"source materialization requires canonical {record_type}:{record_id}"
        )
    if entry.payload != dict(exact_payload):
        raise evidence.PointInTimeEvidenceError(
            f"source materialization {record_type} does not match canonical registry bytes"
        )
    return entry


@dataclass(frozen=True, slots=True)
class SourceFeatureArtifactPublication:
    publication_key: str
    dataset_snapshot_id: str
    dataset_record_sha256: str
    dataset_manifest_sha256: str
    source_identity: str
    feature_set_id: str
    feature_version: str
    feature_record_sha256: str
    feature_definition_sha256: str
    feature_source_sha256: str
    feature_payload_sha256: str
    feature_provenance_sha256: str
    lineage_proof_sha256: str
    first_published_at_utc: str
    publication_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.publication_key, "publication_key")
        for name in (
            "dataset_snapshot_id",
            "source_identity",
            "feature_set_id",
            "feature_version",
        ):
            _text(getattr(self, name), name)
        for name in (
            "dataset_record_sha256",
            "dataset_manifest_sha256",
            "feature_record_sha256",
            "feature_definition_sha256",
            "feature_source_sha256",
            "feature_payload_sha256",
            "feature_provenance_sha256",
            "lineage_proof_sha256",
            "publication_sha256",
        ):
            _sha256(getattr(self, name), name)
        _instant(self.first_published_at_utc, "first_published_at_utc")
        expected_key = _publication_key(
            self.dataset_snapshot_id, self.feature_set_id
        )
        if self.publication_key != expected_key:
            raise evidence.PointInTimeEvidenceError(
                "publication_key does not match dataset/feature identity"
            )
        if self.publication_sha256 != _digest(self.core_payload()):
            raise evidence.PointInTimeEvidenceError(
                "feature publication record digest mismatch"
            )

    def core_payload(self) -> dict[str, str]:
        return {
            "publication_key": self.publication_key,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_record_sha256": self.dataset_record_sha256.lower(),
            "dataset_manifest_sha256": self.dataset_manifest_sha256.lower(),
            "source_identity": self.source_identity,
            "feature_set_id": self.feature_set_id,
            "feature_version": self.feature_version,
            "feature_record_sha256": self.feature_record_sha256.lower(),
            "feature_definition_sha256": self.feature_definition_sha256.lower(),
            "feature_source_sha256": self.feature_source_sha256.lower(),
            "feature_payload_sha256": self.feature_payload_sha256.lower(),
            "feature_provenance_sha256": self.feature_provenance_sha256.lower(),
            "lineage_proof_sha256": self.lineage_proof_sha256.lower(),
            "first_published_at_utc": self.first_published_at_utc,
        }

    def to_payload(self) -> dict[str, str]:
        return {**self.core_payload(), "publication_sha256": self.publication_sha256}

    @classmethod
    def issue(
        cls,
        *,
        dataset_snapshot: DatasetSnapshot,
        dataset_record_sha256: str,
        feature_set: FeatureSet,
        feature_record_sha256: str,
        feature_payload_sha256: str,
        feature_provenance_sha256: str,
        lineage_proof_sha256: str,
        first_published_at_utc: str,
    ) -> "SourceFeatureArtifactPublication":
        core = {
            "publication_key": _publication_key(
                dataset_snapshot.dataset_snapshot_id, feature_set.feature_set_id
            ),
            "dataset_snapshot_id": dataset_snapshot.dataset_snapshot_id,
            "dataset_record_sha256": _sha256(
                dataset_record_sha256, "dataset_record_sha256"
            ),
            "dataset_manifest_sha256": dataset_snapshot.manifest_sha256.lower(),
            "source_identity": dataset_snapshot.source_identity,
            "feature_set_id": feature_set.feature_set_id,
            "feature_version": feature_set.version,
            "feature_record_sha256": _sha256(
                feature_record_sha256, "feature_record_sha256"
            ),
            "feature_definition_sha256": feature_set.definition_sha256.lower(),
            "feature_source_sha256": feature_set.source_sha256.lower(),
            "feature_payload_sha256": _sha256(
                feature_payload_sha256, "feature_payload_sha256"
            ),
            "feature_provenance_sha256": _sha256(
                feature_provenance_sha256, "feature_provenance_sha256"
            ),
            "lineage_proof_sha256": _sha256(
                lineage_proof_sha256, "lineage_proof_sha256"
            ),
            "first_published_at_utc": _text(
                first_published_at_utc, "first_published_at_utc"
            ),
        }
        return cls(**core, publication_sha256=_digest(core))

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any]
    ) -> "SourceFeatureArtifactPublication":
        expected = {
            "publication_key",
            "dataset_snapshot_id",
            "dataset_record_sha256",
            "dataset_manifest_sha256",
            "source_identity",
            "feature_set_id",
            "feature_version",
            "feature_record_sha256",
            "feature_definition_sha256",
            "feature_source_sha256",
            "feature_payload_sha256",
            "feature_provenance_sha256",
            "lineage_proof_sha256",
            "first_published_at_utc",
            "publication_sha256",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise evidence.PointInTimeEvidenceError(
                "feature publication fields mismatch"
            )
        return cls(**payload)


class SourceFeatureArtifactAuthority:
    """Monotonic read authority for source-owned first publication facts."""

    def __init__(self, lineage_authority: DatasetSnapshotLineageAuthority) -> None:
        runtime_repair._require_exact_lineage_authority(lineage_authority)
        self.lineage_authority = lineage_authority
        self.path = lineage_authority.path.with_name(_CANONICAL_FILENAME)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lineage_machine = lineage_authority.monotonic_authority
        self.monotonic_authority = MonotonicWorkspaceAuthority(
            workspace=self.path.parent.resolve(strict=False),
            workspace_instance_id=lineage_machine.workspace_instance_id,
            domain=_MONOTONIC_DOMAIN,
            key=_MONOTONIC_KEY,
            authority_root=lineage_machine.authority_root,
        )
        self._authority_binding_sha256 = _digest(
            {
                "schema": _SCHEMA,
                "schema_version": _SCHEMA_VERSION,
                "lineage_path": str(lineage_authority.path.resolve(strict=False)),
                "registry_path": str(lineage_authority.registry.path.resolve(strict=False)),
                "workspace_instance_id": lineage_machine.workspace_instance_id,
                "authority_root": str(lineage_machine.authority_root.resolve(strict=False)),
            }
        )
        self._bootstrap_or_recover()

    @classmethod
    def for_lineage(
        cls, lineage_authority: DatasetSnapshotLineageAuthority
    ) -> "SourceFeatureArtifactAuthority":
        return cls(lineage_authority)

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> "SourceFeatureArtifactAuthority":
        del workspace
        raise evidence.PointInTimeEvidenceError(
            "source feature artifact authority must be bound to the exact canonical lineage authority"
        )

    @staticmethod
    def _state_core(
        records: Mapping[str, SourceFeatureArtifactPublication]
    ) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "records": {
                key: records[key].to_payload() for key in sorted(records)
            },
        }

    @classmethod
    def _state_sha256(
        cls, records: Mapping[str, SourceFeatureArtifactPublication]
    ) -> str:
        return _digest(cls._state_core(records))

    @classmethod
    def _state_payload(
        cls, records: Mapping[str, SourceFeatureArtifactPublication]
    ) -> dict[str, Any]:
        core = cls._state_core(records)
        return {**core, "state_sha256": _digest(core)}

    def _semantic_binding_sha256(self, state_sha256: str) -> str:
        return _digest(
            {
                "kind": _MONOTONIC_BINDING_KIND,
                "schema_version": 1,
                "authority_binding_sha256": self._authority_binding_sha256,
                "state_sha256": _sha256(state_sha256, "state_sha256"),
            }
        )

    def _load_unlocked(
        self,
    ) -> tuple[dict[str, SourceFeatureArtifactPublication], str]:
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError) as exc:
            raise evidence.PointInTimeEvidenceError(
                "feature artifact authority state is unreadable"
            ) from exc
        try:
            raw = json.loads(
                raw_text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise evidence.PointInTimeEvidenceError(
                "feature artifact authority state is not valid JSON"
            ) from exc
        expected = {"schema", "schema_version", "records", "state_sha256"}
        if type(raw) is not dict or set(raw) != expected:
            raise evidence.PointInTimeEvidenceError(
                "feature artifact authority state fields mismatch"
            )
        if raw["schema"] != _SCHEMA or raw["schema_version"] != _SCHEMA_VERSION:
            raise evidence.PointInTimeEvidenceError(
                "feature artifact authority schema mismatch"
            )
        if type(raw["records"]) is not dict:
            raise evidence.PointInTimeEvidenceError(
                "feature artifact authority records must be an object"
            )
        records: dict[str, SourceFeatureArtifactPublication] = {}
        for key, payload in raw["records"].items():
            canonical_key = _sha256(key, "publication key")
            record = SourceFeatureArtifactPublication.from_payload(payload)
            if record.publication_key != canonical_key:
                raise evidence.PointInTimeEvidenceError(
                    "feature publication map key mismatch"
                )
            records[canonical_key] = record
        observed = self._state_sha256(records)
        if _sha256(raw["state_sha256"], "state_sha256") != observed:
            raise evidence.PointInTimeEvidenceError(
                "feature artifact authority state digest mismatch"
            )
        return records, observed

    def _recover_unlocked(
        self,
        records: Mapping[str, SourceFeatureArtifactPublication],
        observed: str,
    ) -> None:
        del records
        try:
            self.monotonic_authority.recover(observed_state_sha256=observed)
            return
        except MonotonicAuthorityRecoveryRequiredError:
            history = self.monotonic_authority.read_history()
            if not history:
                raise evidence.PointInTimeEvidenceError(
                    "feature artifact authority recovery history disappeared"
                )
            pending = history[-1]
            binding = self._semantic_binding_sha256(observed)
            if (
                pending.intended_state_sha256 != observed
                or pending.semantic_binding_sha256 != binding
            ):
                raise evidence.PointInTimeEvidenceError(
                    "feature artifact authority prepared state cannot be proven"
                )
            try:
                self.monotonic_authority.recover(
                    observed_state_sha256=observed,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise evidence.PointInTimeEvidenceError(
                    "feature artifact authority recovery failed closed"
                ) from exc
        except MonotonicWorkspaceAuthorityError as exc:
            raise evidence.PointInTimeEvidenceError(
                "feature artifact authority monotonic history rejected workspace state"
            ) from exc

    def _read_and_recover_unlocked(
        self,
    ) -> tuple[dict[str, SourceFeatureArtifactPublication], str]:
        records, observed = self._load_unlocked()
        self._recover_unlocked(records, observed)
        return records, observed

    def _bootstrap_or_recover(self) -> None:
        with WorkspaceEconomicLock(self.path.parent):
            if self.path.exists():
                self._read_and_recover_unlocked()
                return
            try:
                self.monotonic_authority.recover(observed_state_sha256=None)
            except MonotonicWorkspaceAuthorityError as exc:
                raise evidence.PointInTimeEvidenceError(
                    "feature artifact authority was deleted or rolled back"
                ) from exc
            records: dict[str, SourceFeatureArtifactPublication] = {}
            intended = self._state_sha256(records)
            binding = self._semantic_binding_sha256(intended)
            tx_id = f"source-feature-bootstrap-{uuid.uuid4().hex}"
            try:
                self.monotonic_authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                atomic_write_json(self.path, self._state_payload(records))
                verified_records, verified = self._load_unlocked()
                if verified_records or verified != intended:
                    raise evidence.PointInTimeEvidenceError(
                        "feature artifact authority bootstrap verification failed"
                    )
                self.monotonic_authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise evidence.PointInTimeEvidenceError(
                    "feature artifact authority bootstrap failed closed"
                ) from exc

    def publish(self, **_: object) -> SourceFeatureArtifactPublication:
        raise evidence.PointInTimeEvidenceError(
            "direct feature publication is forbidden; publication is owned by the canonical source materialization seam"
        )

    def _publish_from_materializer(
        self,
        *,
        materializer: "SourceFeatureArtifactMaterializer",
        dataset_snapshot: DatasetSnapshot,
        dataset_entry: RegistryEntry,
        feature_set: FeatureSet,
        feature_entry: RegistryEntry,
        feature_provenance: provenance_guard.FeatureArtifactProvenance,
        lineage_proof_sha256: str,
    ) -> SourceFeatureArtifactPublication:
        frame = inspect.currentframe()
        caller = None if frame is None else frame.f_back
        if (
            caller is None
            or caller.f_code is not SourceFeatureArtifactMaterializer.materialize.__code__
            or type(materializer) is not SourceFeatureArtifactMaterializer
            or materializer._authority is not self
            or materializer.lineage_authority is not self.lineage_authority
            or materializer._issuance_capability
            is not _HEADLESS_COLLECTOR_ISSUANCE_CAPABILITY
            or materializer._source_service is None
        ):
            raise evidence.PointInTimeEvidenceError(
                "source publication capability may only be exercised by the canonical materializer"
            )
        key = _publication_key(
            dataset_snapshot.dataset_snapshot_id, feature_set.feature_set_id
        )
        with WorkspaceEconomicLock(self.path.parent):
            records, observed = self._read_and_recover_unlocked()
            existing = records.get(key)
            expected_identity = (
                dataset_entry.record_sha256,
                dataset_snapshot.manifest_sha256.lower(),
                dataset_snapshot.source_identity,
                feature_entry.record_sha256,
                feature_set.version,
                feature_set.definition_sha256.lower(),
                feature_set.source_sha256.lower(),
                feature_provenance.feature_payload_sha256,
                feature_provenance.provenance_sha256,
                _sha256(lineage_proof_sha256, "lineage_proof_sha256"),
            )
            if existing is not None:
                actual_identity = (
                    existing.dataset_record_sha256,
                    existing.dataset_manifest_sha256,
                    existing.source_identity,
                    existing.feature_record_sha256,
                    existing.feature_version,
                    existing.feature_definition_sha256,
                    existing.feature_source_sha256,
                    existing.feature_payload_sha256,
                    existing.feature_provenance_sha256,
                    existing.lineage_proof_sha256,
                )
                if actual_identity != expected_identity:
                    raise evidence.PointInTimeEvidenceError(
                        "feature artifact publication identity cannot be rebound"
                    )
                return existing
            record = SourceFeatureArtifactPublication.issue(
                dataset_snapshot=dataset_snapshot,
                dataset_record_sha256=dataset_entry.record_sha256,
                feature_set=feature_set,
                feature_record_sha256=feature_entry.record_sha256,
                feature_payload_sha256=feature_provenance.feature_payload_sha256,
                feature_provenance_sha256=feature_provenance.provenance_sha256,
                lineage_proof_sha256=lineage_proof_sha256,
                first_published_at_utc=(
                    __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    )
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                ),
            )
            updated = dict(records)
            updated[key] = record
            intended = self._state_sha256(updated)
            binding = self._semantic_binding_sha256(intended)
            tx_id = f"source-feature-publication-{uuid.uuid4().hex}"
            try:
                self.monotonic_authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                atomic_write_json(self.path, self._state_payload(updated))
                verified_records, verified = self._load_unlocked()
                if verified != intended or verified_records.get(key) != record:
                    raise evidence.PointInTimeEvidenceError(
                        "published feature artifact authority state digest mismatch"
                    )
                self.monotonic_authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise evidence.PointInTimeEvidenceError(
                    "feature artifact publication transaction failed closed"
                ) from exc
            return record

    def resolve(
        self,
        *,
        dataset_snapshot: DatasetSnapshot,
        feature_set: FeatureSet,
        feature_provenance: provenance_guard.FeatureArtifactProvenance,
    ) -> SourceFeatureArtifactPublication:
        if type(dataset_snapshot) is not DatasetSnapshot:
            raise evidence.PointInTimeEvidenceError(
                "dataset_snapshot must be an exact DatasetSnapshot"
            )
        if type(feature_set) is not FeatureSet:
            raise evidence.PointInTimeEvidenceError(
                "feature_set must be an exact FeatureSet"
            )
        if type(feature_provenance) is not provenance_guard.FeatureArtifactProvenance:
            raise evidence.PointInTimeEvidenceError(
                "feature_provenance must be exact FeatureArtifactProvenance"
            )
        dataset_entry = _registry_entry(
            self.lineage_authority,
            record_type="DatasetSnapshot",
            record_id=dataset_snapshot.dataset_snapshot_id,
            exact_payload=dataset_snapshot.to_payload(),
        )
        feature_entry = _registry_entry(
            self.lineage_authority,
            record_type="FeatureSet",
            record_id=feature_set.feature_set_id,
            exact_payload=feature_set.to_payload(),
        )
        lineage_record = self.lineage_authority.record(
            dataset_snapshot.dataset_snapshot_id
        )
        if lineage_record.dataset_record_sha256 != dataset_entry.record_sha256:
            raise evidence.PointInTimeEvidenceError(
                "source publication dataset lineage does not match canonical registry record"
            )
        if feature_provenance.provenance_sha256 not in lineage_record.member_sha256:
            raise evidence.PointInTimeEvidenceError(
                "source publication provenance is not a member of canonical dataset lineage"
            )
        key = _publication_key(
            dataset_snapshot.dataset_snapshot_id, feature_set.feature_set_id
        )
        with WorkspaceEconomicLock(self.path.parent):
            records, _ = self._read_and_recover_unlocked()
        record = records.get(key)
        if record is None:
            raise evidence.PointInTimeEvidenceError(
                "positive point-in-time feature evidence requires an independent source-owned feature artifact authority"
            )
        expected = (
            dataset_entry.record_sha256,
            dataset_snapshot.manifest_sha256.lower(),
            dataset_snapshot.source_identity,
            feature_entry.record_sha256,
            feature_set.version,
            feature_set.definition_sha256.lower(),
            feature_set.source_sha256.lower(),
            feature_provenance.feature_payload_sha256,
            feature_provenance.provenance_sha256,
            lineage_record.proof_sha256,
        )
        actual = (
            record.dataset_record_sha256,
            record.dataset_manifest_sha256,
            record.source_identity,
            record.feature_record_sha256,
            record.feature_version,
            record.feature_definition_sha256,
            record.feature_source_sha256,
            record.feature_payload_sha256,
            record.feature_provenance_sha256,
            record.lineage_proof_sha256,
        )
        if actual != expected:
            raise evidence.PointInTimeEvidenceError(
                "source-owned feature artifact authority does not match exact canonical feature lineage"
            )
        return record


class SourceFeatureArtifactMaterializer:
    """Canonical source-side producer over durable collector truth.

    The writer never accepts caller-authored feature bytes or publication time.
    It re-resolves one exact immutable CollectorDelta, deterministically builds
    the supported artifact, and only then exercises first-publication authority.
    """

    def __init__(
        self,
        lineage_authority: DatasetSnapshotLineageAuthority,
        *,
        collector_store: CollectorDeltaStore,
        _issuance_capability: object | None = None,
        _source_service: object | None = None,
    ) -> None:
        runtime_repair._require_exact_lineage_authority(lineage_authority)
        self.lineage_authority = lineage_authority
        self.collector_store = _require_canonical_collector_store(
            lineage_authority, collector_store
        )
        self._issuance_capability = _issuance_capability
        self._source_service = _source_service
        self._authority = SourceFeatureArtifactAuthority.for_lineage(
            lineage_authority
        )

    @property
    def authority(self) -> SourceFeatureArtifactAuthority:
        return self._authority

    def materialize(
        self,
        *,
        dataset_snapshot: DatasetSnapshot,
        feature_set: FeatureSet,
        source_delta_id: str | None = None,
        feature_payload: bytes | None = None,
    ) -> SourceFeatureArtifactPublication:
        if (
            self._issuance_capability
            is not _HEADLESS_COLLECTOR_ISSUANCE_CAPABILITY
            or self._source_service is None
        ):
            raise evidence.PointInTimeEvidenceError(
                "source feature publication requires issuance by the canonical "
                "HeadlessCollectorService source runtime"
            )
        from .collector_service import HeadlessCollectorService

        if type(self._source_service) is not HeadlessCollectorService:
            raise evidence.PointInTimeEvidenceError(
                "source feature publication requires the exact canonical "
                "HeadlessCollectorService"
            )
        if self._source_service.delta_store is not self.collector_store:
            raise evidence.PointInTimeEvidenceError(
                "source feature materializer is not bound to the collector service store"
            )
        source_service = self._source_service._require_source_identity()

        if feature_payload is not None:
            raise evidence.PointInTimeEvidenceError(
                "caller-supplied feature_payload is forbidden; "
                "source feature bytes are produced from canonical CollectorDeltaStore truth"
            )
        if type(dataset_snapshot) is not DatasetSnapshot:
            raise evidence.PointInTimeEvidenceError(
                "dataset_snapshot must be an exact DatasetSnapshot"
            )
        if type(feature_set) is not FeatureSet:
            raise evidence.PointInTimeEvidenceError(
                "feature_set must be an exact FeatureSet"
            )
        if source_delta_id is None:
            raise evidence.PointInTimeEvidenceError(
                "source_delta_id is required for source-owned feature materialization"
            )
        source_delta_id = _text(source_delta_id, "source_delta_id")
        source_delta = type(self.collector_store).get(
            self.collector_store, source_delta_id
        )
        if source_delta is None:
            raise evidence.PointInTimeEvidenceError(
                "source_delta_id is not present in the canonical collector store"
            )
        if (
            source_delta.source_id != self._source_service.source_id
            or source_delta.source_id != source_service.source_id
            or source_delta.stream_epoch != source_service.stream_epoch
        ):
            raise evidence.PointInTimeEvidenceError(
                "source feature publication delta is outside the active collector "
                "service source/epoch authority"
            )

        _require_source_ingestion_receipt(self.collector_store, source_delta)

        generated_payload = collector_source_feature_payload(
            source_delta=source_delta,
            dataset_snapshot=dataset_snapshot,
            feature_set=feature_set,
        )
        dataset_entry = _registry_entry(
            self.lineage_authority,
            record_type="DatasetSnapshot",
            record_id=dataset_snapshot.dataset_snapshot_id,
            exact_payload=dataset_snapshot.to_payload(),
        )
        feature_entry = _registry_entry(
            self.lineage_authority,
            record_type="FeatureSet",
            record_id=feature_set.feature_set_id,
            exact_payload=feature_set.to_payload(),
        )
        feature_provenance = provenance_guard.FeatureArtifactProvenance.issue(
            dataset_snapshot=dataset_snapshot,
            feature_set=feature_set,
            feature_payload=generated_payload,
        )
        lineage_record = self.lineage_authority.record(
            dataset_snapshot.dataset_snapshot_id
        )
        if lineage_record.dataset_record_sha256 != dataset_entry.record_sha256:
            raise evidence.PointInTimeEvidenceError(
                "source materialization dataset lineage does not match canonical registry record"
            )
        if feature_provenance.provenance_sha256 not in lineage_record.member_sha256:
            raise evidence.PointInTimeEvidenceError(
                "source materialization provenance is not a member of canonical dataset lineage"
            )

        from datetime import datetime as _AuthorityDateTime
        from datetime import timezone as _AuthorityTimezone

        authority_now = _AuthorityDateTime.now(_AuthorityTimezone.utc)
        if authority_now < _instant(
            source_delta.desktop_available_at, "source_delta.desktop_available_at"
        ):
            raise evidence.PointInTimeEvidenceError(
                "authority clock predates durable collector source availability"
            )

        return self._authority._publish_from_materializer(
            materializer=self,
            dataset_snapshot=dataset_snapshot,
            dataset_entry=dataset_entry,
            feature_set=feature_set,
            feature_entry=feature_entry,
            feature_provenance=feature_provenance,
            lineage_proof_sha256=lineage_record.proof_sha256,
        )

def _materializer_from_headless_collector_service(
    *,
    service: object,
    lineage_authority: DatasetSnapshotLineageAuthority,
) -> SourceFeatureArtifactMaterializer:
    """Issue one source-feature writer capability from the real collector runtime.

    This is intentionally private.  The caller must be the method installed on the
    exact canonical HeadlessCollectorService class; an evaluator importing this
    helper directly cannot turn exact DTOs/store bytes into publication authority.
    """

    from .collector_service import HeadlessCollectorService

    if type(service) is not HeadlessCollectorService:
        raise evidence.PointInTimeEvidenceError(
            "feature materializer issuance requires exact HeadlessCollectorService"
        )
    source_method = getattr(
        HeadlessCollectorService, "source_feature_materializer", None
    )
    frame = inspect.currentframe()
    caller = None if frame is None else frame.f_back
    if (
        source_method is None
        or caller is None
        or caller.f_code is not getattr(source_method, "__code__", None)
    ):
        raise evidence.PointInTimeEvidenceError(
            "feature materializer capability may only be issued by "
            "HeadlessCollectorService.source_feature_materializer"
        )
    service._require_source_identity()
    return SourceFeatureArtifactMaterializer(
        lineage_authority,
        collector_store=service.delta_store,
        _issuance_capability=_HEADLESS_COLLECTOR_ISSUANCE_CAPABILITY,
        _source_service=service,
    )


def _same_lineage_authority(
    left: DatasetSnapshotLineageAuthority,
    right: DatasetSnapshotLineageAuthority,
) -> bool:
    left_machine = left.monotonic_authority
    right_machine = right.monotonic_authority
    return (
        left.path.resolve(strict=False) == right.path.resolve(strict=False)
        and left.registry.path.resolve(strict=False)
        == right.registry.path.resolve(strict=False)
        and left_machine.workspace_instance_id == right_machine.workspace_instance_id
        and left_machine.authority_root.resolve(strict=False)
        == right_machine.authority_root.resolve(strict=False)
    )


def _bind_with_source_authority(
    *,
    dataset_snapshot: DatasetSnapshot,
    feature_set: FeatureSet,
    feature_provenance,
    lineage_authority,
    decision_cutoff_utc: str,
    feature_payload_sha256: str | None = None,
    feature_artifact_authority: SourceFeatureArtifactAuthority | None = None,
):
    runtime_repair._require_exact_lineage_authority(lineage_authority)
    if type(feature_artifact_authority) is not SourceFeatureArtifactAuthority:
        raise evidence.PointInTimeEvidenceError(
            "positive point-in-time feature evidence requires an independent source-owned feature artifact authority"
        )
    if not _same_lineage_authority(
        feature_artifact_authority.lineage_authority, lineage_authority
    ):
        raise evidence.PointInTimeEvidenceError(
            "feature artifact authority must be bound to the exact canonical lineage authority"
        )
    if type(feature_provenance) is not provenance_guard.FeatureArtifactProvenance:
        raise evidence.PointInTimeEvidenceError(
            "feature_provenance must be exact FeatureArtifactProvenance"
        )
    payload_sha = feature_provenance.feature_payload_sha256
    if (
        feature_payload_sha256 is not None
        and _sha256(feature_payload_sha256, "feature_payload_sha256") != payload_sha
    ):
        raise evidence.PointInTimeEvidenceError(
            "feature payload audit digest does not match canonical provenance"
        )
    publication = feature_artifact_authority.resolve(
        dataset_snapshot=dataset_snapshot,
        feature_set=feature_set,
        feature_provenance=feature_provenance,
    )
    decision_cutoff = _instant(decision_cutoff_utc, "decision_cutoff_utc")
    publication_time = _instant(
        publication.first_published_at_utc, "first_published_at_utc"
    )
    if publication_time > decision_cutoff:
        raise evidence.FutureEvidenceError(
            "feature artifact was not source-published by decision cutoff"
        )
    base = provenance_guard._bind(
        dataset_snapshot=dataset_snapshot,
        feature_set=feature_set,
        feature_provenance=feature_provenance,
        lineage_authority=lineage_authority,
        decision_cutoff_utc=decision_cutoff_utc,
        feature_payload_sha256=payload_sha,
    )
    available_at = max(
        _instant(base.available_at_utc, "available_at_utc"), publication_time
    ).isoformat().replace("+00:00", "Z")
    return provenance_guard.FeatureAvailabilityEvidence(
        feature_identity=base.feature_identity,
        feature_version=base.feature_version,
        feature_definition_sha256=base.feature_definition_sha256,
        source_identity=base.source_identity,
        source_revision=base.source_revision,
        revision_policy_id=base.revision_policy_id,
        dataset_snapshot_id=base.dataset_snapshot_id,
        dataset_record_sha256=base.dataset_record_sha256,
        dataset_manifest_sha256=base.dataset_manifest_sha256,
        dataset_lineage_proof_sha256=base.dataset_lineage_proof_sha256,
        feature_provenance_sha256=base.feature_provenance_sha256,
        feature_payload_sha256=base.feature_payload_sha256,
        as_of_utc=base.as_of_utc,
        available_at_utc=available_at,
        decision_cutoff_utc=base.decision_cutoff_utc,
    )


evidence.SourceFeatureArtifactPublication = SourceFeatureArtifactPublication
evidence.SourceFeatureArtifactAuthority = SourceFeatureArtifactAuthority
evidence.PointInTimeFeatureAuthority.bind = staticmethod(_bind_with_source_authority)

__all__ = [
    "COLLECTOR_SOURCE_FEATURE_DEFINITION_SHA256",
    "COLLECTOR_SOURCE_FEATURE_PRODUCER_ID",
    "COLLECTOR_SOURCE_FEATURE_PRODUCER_SHA256",
    "SourceFeatureArtifactPublication",
    "SourceFeatureArtifactAuthority",
    "collector_source_feature_payload",
]
