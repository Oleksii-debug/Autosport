"""Source-owned first-publication authority for point-in-time feature artifacts.

The evaluator must never be able to manufacture an earlier feature-availability time.
This store records the first product-observed publication of one exact
DatasetSnapshot/FeatureSet/payload tuple behind a durable path lock.  The public
point-in-time binder consumes that persisted record and treats caller values only as
audit assertions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import _point_in_time_authority_runtime_repair as runtime_repair
from . import _point_in_time_feature_provenance_guard as provenance_guard
from . import point_in_time_evidence as evidence
from .integrity import atomic_write_json, durable_path_lock
from .scientific_registry import DatasetSnapshot, FeatureSet

_SCHEMA = "autosport.source-feature-artifact-authority"
_SCHEMA_VERSION = 1
_CANONICAL_FILENAME = "source-feature-artifact-publications.json"
_HEX = frozenset("0123456789abcdef")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise evidence.PointInTimeEvidenceError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise evidence.PointInTimeEvidenceError(f"{name} must be a canonical SHA-256 hex string")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise evidence.PointInTimeEvidenceError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise evidence.PointInTimeEvidenceError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _publication_key(dataset_snapshot_id: str, feature_set_id: str) -> str:
    return _digest(
        {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "dataset_snapshot_id": _text(dataset_snapshot_id, "dataset_snapshot_id"),
            "feature_set_id": _text(feature_set_id, "feature_set_id"),
        }
    )


@dataclass(frozen=True, slots=True)
class SourceFeatureArtifactPublication:
    publication_key: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    source_identity: str
    feature_set_id: str
    feature_version: str
    feature_definition_sha256: str
    feature_source_sha256: str
    feature_payload_sha256: str
    first_published_at_utc: str

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
            "dataset_manifest_sha256",
            "feature_definition_sha256",
            "feature_source_sha256",
            "feature_payload_sha256",
        ):
            _sha256(getattr(self, name), name)
        _instant(self.first_published_at_utc, "first_published_at_utc")
        expected = _publication_key(self.dataset_snapshot_id, self.feature_set_id)
        if self.publication_key != expected:
            raise evidence.PointInTimeEvidenceError("publication_key does not match dataset/feature identity")

    def to_payload(self) -> dict[str, str]:
        return {
            "publication_key": self.publication_key,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_manifest_sha256": self.dataset_manifest_sha256.lower(),
            "source_identity": self.source_identity,
            "feature_set_id": self.feature_set_id,
            "feature_version": self.feature_version,
            "feature_definition_sha256": self.feature_definition_sha256.lower(),
            "feature_source_sha256": self.feature_source_sha256.lower(),
            "feature_payload_sha256": self.feature_payload_sha256.lower(),
            "first_published_at_utc": self.first_published_at_utc,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SourceFeatureArtifactPublication":
        expected = {
            "publication_key",
            "dataset_snapshot_id",
            "dataset_manifest_sha256",
            "source_identity",
            "feature_set_id",
            "feature_version",
            "feature_definition_sha256",
            "feature_source_sha256",
            "feature_payload_sha256",
            "first_published_at_utc",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise evidence.PointInTimeEvidenceError("feature publication fields mismatch")
        return cls(**payload)


class SourceFeatureArtifactAuthority:
    """Durable first-publication store owned by feature materialization/ingestion."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.name != _CANONICAL_FILENAME:
            raise evidence.PointInTimeEvidenceError(
                f"feature artifact authority path must end with {_CANONICAL_FILENAME}"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with durable_path_lock(self.path):
            self._load_unlocked()

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> "SourceFeatureArtifactAuthority":
        return cls(Path(workspace) / _CANONICAL_FILENAME)

    def _empty_payload(self) -> dict[str, Any]:
        core = {"schema": _SCHEMA, "schema_version": _SCHEMA_VERSION, "records": {}}
        return {**core, "state_sha256": _digest(core)}

    def _load_unlocked(self) -> dict[str, SourceFeatureArtifactPublication]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise evidence.PointInTimeEvidenceError("feature artifact authority state is unreadable") from exc
        expected = {"schema", "schema_version", "records", "state_sha256"}
        if type(raw) is not dict or set(raw) != expected:
            raise evidence.PointInTimeEvidenceError("feature artifact authority state fields mismatch")
        if raw["schema"] != _SCHEMA or raw["schema_version"] != _SCHEMA_VERSION:
            raise evidence.PointInTimeEvidenceError("feature artifact authority schema mismatch")
        if type(raw["records"]) is not dict:
            raise evidence.PointInTimeEvidenceError("feature artifact authority records must be an object")
        core = {"schema": raw["schema"], "schema_version": raw["schema_version"], "records": raw["records"]}
        if _sha256(raw["state_sha256"], "state_sha256") != _digest(core):
            raise evidence.PointInTimeEvidenceError("feature artifact authority state digest mismatch")
        records: dict[str, SourceFeatureArtifactPublication] = {}
        for key, payload in raw["records"].items():
            key = _sha256(key, "publication key")
            record = SourceFeatureArtifactPublication.from_payload(payload)
            if record.publication_key != key:
                raise evidence.PointInTimeEvidenceError("feature publication map key mismatch")
            records[key] = record
        return records

    def _write_unlocked(self, records: Mapping[str, SourceFeatureArtifactPublication]) -> None:
        body = {key: records[key].to_payload() for key in sorted(records)}
        core: dict[str, Any] = {"schema": _SCHEMA, "schema_version": _SCHEMA_VERSION, "records": body}
        atomic_write_json(self.path, {**core, "state_sha256": _digest(core)})

    def publish(
        self,
        *,
        dataset_snapshot: DatasetSnapshot,
        feature_set: FeatureSet,
        feature_payload: bytes,
    ) -> SourceFeatureArtifactPublication:
        if type(dataset_snapshot) is not DatasetSnapshot:
            raise evidence.PointInTimeEvidenceError("dataset_snapshot must be an exact DatasetSnapshot")
        if type(feature_set) is not FeatureSet:
            raise evidence.PointInTimeEvidenceError("feature_set must be an exact FeatureSet")
        if type(feature_payload) is not bytes or not feature_payload:
            raise evidence.PointInTimeEvidenceError("feature_payload must be non-empty immutable bytes")
        key = _publication_key(dataset_snapshot.dataset_snapshot_id, feature_set.feature_set_id)
        payload_sha = hashlib.sha256(feature_payload).hexdigest()
        with durable_path_lock(self.path):
            records = self._load_unlocked()
            existing = records.get(key)
            if existing is not None:
                expected = (
                    dataset_snapshot.manifest_sha256.lower(),
                    dataset_snapshot.source_identity,
                    feature_set.version,
                    feature_set.definition_sha256.lower(),
                    feature_set.source_sha256.lower(),
                    payload_sha,
                )
                actual = (
                    existing.dataset_manifest_sha256,
                    existing.source_identity,
                    existing.feature_version,
                    existing.feature_definition_sha256,
                    existing.feature_source_sha256,
                    existing.feature_payload_sha256,
                )
                if actual != expected:
                    raise evidence.PointInTimeEvidenceError(
                        "feature artifact publication identity cannot be rebound"
                    )
                return existing
            record = SourceFeatureArtifactPublication(
                publication_key=key,
                dataset_snapshot_id=dataset_snapshot.dataset_snapshot_id,
                dataset_manifest_sha256=dataset_snapshot.manifest_sha256.lower(),
                source_identity=dataset_snapshot.source_identity,
                feature_set_id=feature_set.feature_set_id,
                feature_version=feature_set.version,
                feature_definition_sha256=feature_set.definition_sha256.lower(),
                feature_source_sha256=feature_set.source_sha256.lower(),
                feature_payload_sha256=payload_sha,
                first_published_at_utc=_utc_now(),
            )
            records[key] = record
            self._write_unlocked(records)
            return record

    def resolve(
        self,
        *,
        dataset_snapshot: DatasetSnapshot,
        feature_set: FeatureSet,
        feature_payload_sha256: str,
    ) -> SourceFeatureArtifactPublication:
        key = _publication_key(dataset_snapshot.dataset_snapshot_id, feature_set.feature_set_id)
        with durable_path_lock(self.path):
            records = self._load_unlocked()
        record = records.get(key)
        if record is None:
            raise evidence.PointInTimeEvidenceError(
                "positive point-in-time feature evidence requires an independent source-owned feature artifact authority"
            )
        expected = (
            dataset_snapshot.manifest_sha256.lower(),
            dataset_snapshot.source_identity,
            feature_set.version,
            feature_set.definition_sha256.lower(),
            feature_set.source_sha256.lower(),
            _sha256(feature_payload_sha256, "feature_payload_sha256"),
        )
        actual = (
            record.dataset_manifest_sha256,
            record.source_identity,
            record.feature_version,
            record.feature_definition_sha256,
            record.feature_source_sha256,
            record.feature_payload_sha256,
        )
        if actual != expected:
            raise evidence.PointInTimeEvidenceError("source-owned feature artifact authority does not match exact feature")
        return record


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
    if feature_artifact_authority.path.parent.resolve(strict=False) != lineage_authority.path.parent.resolve(strict=False):
        raise evidence.PointInTimeEvidenceError("feature artifact authority must belong to the canonical lineage workspace")
    payload_sha = feature_provenance.feature_payload_sha256
    if feature_payload_sha256 is not None and _sha256(feature_payload_sha256, "feature_payload_sha256") != payload_sha:
        raise evidence.PointInTimeEvidenceError("feature payload audit digest does not match canonical provenance")
    publication = feature_artifact_authority.resolve(
        dataset_snapshot=dataset_snapshot,
        feature_set=feature_set,
        feature_payload_sha256=payload_sha,
    )
    decision_cutoff = _instant(decision_cutoff_utc, "decision_cutoff_utc")
    publication_time = _instant(publication.first_published_at_utc, "first_published_at_utc")
    if publication_time > decision_cutoff:
        raise evidence.FutureEvidenceError("feature artifact was not source-published by decision cutoff")
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

__all__ = ["SourceFeatureArtifactPublication", "SourceFeatureArtifactAuthority"]
