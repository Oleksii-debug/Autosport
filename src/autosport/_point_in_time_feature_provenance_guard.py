"""Canonical dataset/feature/artifact provenance for point-in-time evidence.

This is a narrow compatibility guard over ``point_in_time_evidence``.  It does not
create a second feature store or scientific registry.  Instead, it makes one typed
feature-artifact provenance commitment a member of the existing immutable
``DatasetSnapshotLineageAuthority`` membership manifest.  Positive point-in-time
evidence therefore requires the exact registered DatasetSnapshot, exact registered
FeatureSet, exact feature payload digest, and authority-owned lineage publication to
have existed by the decision cutoff.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from . import point_in_time_evidence as evidence
from .dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from .scientific_registry import DatasetSnapshot, FeatureSet


_PROVENANCE_KIND = "autosport-dataset-feature-artifact-provenance-v1"
_REVISION_POLICY_ID = "dataset-lineage-feature-artifact-v1"
_SCHEMA_VERSION = 1


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise evidence.PointInTimeEvidenceError(
            f"{name} must be a non-empty canonical string"
        )
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in evidence._HEX for char in text):
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


def _utc(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _digest(payload: Mapping[str, Any]) -> str:
    return evidence._digest(payload)


@dataclass(frozen=True, slots=True)
class FeatureArtifactProvenance:
    """Typed commitment joining one dataset snapshot to one exact feature artifact.

    ``issue`` accepts feature bytes, not a caller-authored digest.  The resulting
    provenance digest becomes a member of the canonical DatasetSnapshot membership
    manifest.  A reconstructed record is useful only when that exact digest is
    already present in the independently verified lineage authority.
    """

    dataset_snapshot_id: str
    source_identity: str
    license_identity: str
    dataset_causal_cutoff: str
    dataset_available_at_utc: str
    feature_set_id: str
    feature_version: str
    feature_definition_sha256: str
    feature_source_sha256: str
    feature_available_at_utc: str
    feature_payload_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "dataset_snapshot_id",
            "source_identity",
            "license_identity",
            "feature_set_id",
            "feature_version",
        ):
            _text(getattr(self, name), name)
        for name in (
            "feature_definition_sha256",
            "feature_source_sha256",
            "feature_payload_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(
            self,
            "dataset_causal_cutoff",
            _utc(self.dataset_causal_cutoff, "dataset_causal_cutoff"),
        )
        object.__setattr__(
            self,
            "dataset_available_at_utc",
            _utc(self.dataset_available_at_utc, "dataset_available_at_utc"),
        )
        object.__setattr__(
            self,
            "feature_available_at_utc",
            _utc(self.feature_available_at_utc, "feature_available_at_utc"),
        )

    def core_payload(self) -> dict[str, Any]:
        return {
            "kind": _PROVENANCE_KIND,
            "schema_version": _SCHEMA_VERSION,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "source_identity": self.source_identity,
            "license_identity": self.license_identity,
            "dataset_causal_cutoff": self.dataset_causal_cutoff,
            "dataset_available_at_utc": self.dataset_available_at_utc,
            "feature_set_id": self.feature_set_id,
            "feature_version": self.feature_version,
            "feature_definition_sha256": self.feature_definition_sha256,
            "feature_source_sha256": self.feature_source_sha256,
            "feature_available_at_utc": self.feature_available_at_utc,
            "feature_payload_sha256": self.feature_payload_sha256,
        }

    @property
    def provenance_sha256(self) -> str:
        return _digest(self.core_payload())

    def to_payload(self) -> dict[str, Any]:
        return {**self.core_payload(), "provenance_sha256": self.provenance_sha256}

    @classmethod
    def issue(
        cls,
        *,
        dataset_snapshot: DatasetSnapshot,
        feature_set: FeatureSet,
        feature_payload: bytes,
    ) -> "FeatureArtifactProvenance":
        if type(dataset_snapshot) is not DatasetSnapshot:
            raise evidence.PointInTimeEvidenceError(
                "dataset_snapshot must be an exact DatasetSnapshot"
            )
        if type(feature_set) is not FeatureSet:
            raise evidence.PointInTimeEvidenceError(
                "feature_set must be an exact FeatureSet"
            )
        if type(feature_payload) is not bytes or not feature_payload:
            raise evidence.PointInTimeEvidenceError(
                "feature_payload must be non-empty immutable bytes"
            )
        return cls(
            dataset_snapshot_id=dataset_snapshot.dataset_snapshot_id,
            source_identity=dataset_snapshot.source_identity,
            license_identity=dataset_snapshot.license_identity,
            dataset_causal_cutoff=dataset_snapshot.causal_cutoff,
            dataset_available_at_utc=dataset_snapshot.available_at_utc,
            feature_set_id=feature_set.feature_set_id,
            feature_version=feature_set.version,
            feature_definition_sha256=feature_set.definition_sha256,
            feature_source_sha256=feature_set.source_sha256,
            feature_available_at_utc=feature_set.available_at_utc,
            feature_payload_sha256=hashlib.sha256(feature_payload).hexdigest(),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FeatureArtifactProvenance":
        expected = {
            "kind",
            "schema_version",
            "dataset_snapshot_id",
            "source_identity",
            "license_identity",
            "dataset_causal_cutoff",
            "dataset_available_at_utc",
            "feature_set_id",
            "feature_version",
            "feature_definition_sha256",
            "feature_source_sha256",
            "feature_available_at_utc",
            "feature_payload_sha256",
            "provenance_sha256",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise evidence.PointInTimeEvidenceError(
                "feature artifact provenance fields mismatch"
            )
        if payload["kind"] != _PROVENANCE_KIND or payload["schema_version"] != 1:
            raise evidence.PointInTimeEvidenceError(
                "feature artifact provenance schema mismatch"
            )
        record = cls(
            dataset_snapshot_id=payload["dataset_snapshot_id"],
            source_identity=payload["source_identity"],
            license_identity=payload["license_identity"],
            dataset_causal_cutoff=payload["dataset_causal_cutoff"],
            dataset_available_at_utc=payload["dataset_available_at_utc"],
            feature_set_id=payload["feature_set_id"],
            feature_version=payload["feature_version"],
            feature_definition_sha256=payload["feature_definition_sha256"],
            feature_source_sha256=payload["feature_source_sha256"],
            feature_available_at_utc=payload["feature_available_at_utc"],
            feature_payload_sha256=payload["feature_payload_sha256"],
        )
        if _sha256(payload["provenance_sha256"], "provenance_sha256") != record.provenance_sha256:
            raise evidence.PointInTimeEvidenceError(
                "feature artifact provenance digest mismatch"
            )
        return record


@dataclass(frozen=True, slots=True)
class FeatureAvailabilityEvidence:
    """Positive feature evidence bound to exact canonical registry and lineage facts."""

    feature_identity: str
    feature_version: str
    feature_definition_sha256: str
    source_identity: str
    source_revision: str
    revision_policy_id: str
    dataset_snapshot_id: str
    dataset_record_sha256: str
    dataset_manifest_sha256: str
    dataset_lineage_proof_sha256: str
    feature_provenance_sha256: str
    feature_payload_sha256: str
    as_of_utc: str
    available_at_utc: str
    decision_cutoff_utc: str

    def __post_init__(self) -> None:
        for name in (
            "feature_identity",
            "feature_version",
            "source_identity",
            "revision_policy_id",
            "dataset_snapshot_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "feature_definition_sha256",
            "source_revision",
            "dataset_record_sha256",
            "dataset_manifest_sha256",
            "dataset_lineage_proof_sha256",
            "feature_provenance_sha256",
            "feature_payload_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(self, "as_of_utc", _utc(self.as_of_utc, "as_of_utc"))
        object.__setattr__(
            self,
            "available_at_utc",
            _utc(self.available_at_utc, "available_at_utc"),
        )
        object.__setattr__(
            self,
            "decision_cutoff_utc",
            _utc(self.decision_cutoff_utc, "decision_cutoff_utc"),
        )
        if _instant(self.as_of_utc, "as_of_utc") > _instant(
            self.decision_cutoff_utc, "decision_cutoff_utc"
        ):
            raise evidence.FutureEvidenceError("feature as_of is after decision cutoff")
        if _instant(self.available_at_utc, "available_at_utc") > _instant(
            self.decision_cutoff_utc, "decision_cutoff_utc"
        ):
            raise evidence.FutureEvidenceError(
                "feature was not available by decision cutoff"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "feature_identity": self.feature_identity,
            "feature_version": self.feature_version,
            "feature_definition_sha256": self.feature_definition_sha256,
            "source_identity": self.source_identity,
            "source_revision": self.source_revision,
            "revision_policy_id": self.revision_policy_id,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_record_sha256": self.dataset_record_sha256,
            "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "dataset_lineage_proof_sha256": self.dataset_lineage_proof_sha256,
            "feature_provenance_sha256": self.feature_provenance_sha256,
            "feature_payload_sha256": self.feature_payload_sha256,
            "as_of_utc": self.as_of_utc,
            "available_at_utc": self.available_at_utc,
            "decision_cutoff_utc": self.decision_cutoff_utc,
        }

    @property
    def evidence_id(self) -> str:
        return _digest(self.to_payload())


def _require_exact_registry_records(
    *,
    lineage_authority: DatasetSnapshotLineageAuthority,
    dataset_snapshot: DatasetSnapshot,
    feature_set: FeatureSet,
) -> None:
    dataset_entry = lineage_authority.registry.get(
        "DatasetSnapshot", dataset_snapshot.dataset_snapshot_id
    )
    if dataset_entry is None or dataset_entry.payload != dataset_snapshot.to_payload():
        raise evidence.PointInTimeEvidenceError(
            "dataset snapshot is not the exact canonical registry record"
        )
    feature_entry = lineage_authority.registry.get("FeatureSet", feature_set.feature_set_id)
    if feature_entry is None or feature_entry.payload != feature_set.to_payload():
        raise evidence.PointInTimeEvidenceError(
            "feature set is not the exact canonical registry record"
        )


def _require_provenance_matches(
    provenance: FeatureArtifactProvenance,
    *,
    dataset_snapshot: DatasetSnapshot,
    feature_set: FeatureSet,
) -> None:
    expected = {
        "dataset_snapshot_id": dataset_snapshot.dataset_snapshot_id,
        "source_identity": dataset_snapshot.source_identity,
        "license_identity": dataset_snapshot.license_identity,
        "dataset_causal_cutoff": _utc(
            dataset_snapshot.causal_cutoff, "dataset_snapshot.causal_cutoff"
        ),
        "dataset_available_at_utc": _utc(
            dataset_snapshot.available_at_utc, "dataset_snapshot.available_at"
        ),
        "feature_set_id": feature_set.feature_set_id,
        "feature_version": feature_set.version,
        "feature_definition_sha256": feature_set.definition_sha256.lower(),
        "feature_source_sha256": feature_set.source_sha256.lower(),
        "feature_available_at_utc": _utc(
            feature_set.available_at_utc, "feature_set.available_at"
        ),
    }
    actual = {
        "dataset_snapshot_id": provenance.dataset_snapshot_id,
        "source_identity": provenance.source_identity,
        "license_identity": provenance.license_identity,
        "dataset_causal_cutoff": provenance.dataset_causal_cutoff,
        "dataset_available_at_utc": provenance.dataset_available_at_utc,
        "feature_set_id": provenance.feature_set_id,
        "feature_version": provenance.feature_version,
        "feature_definition_sha256": provenance.feature_definition_sha256,
        "feature_source_sha256": provenance.feature_source_sha256,
        "feature_available_at_utc": provenance.feature_available_at_utc,
    }
    if actual != expected:
        raise evidence.PointInTimeEvidenceError(
            "feature artifact provenance does not match exact dataset/FeatureSet"
        )


def _bind(
    *,
    dataset_snapshot: DatasetSnapshot,
    feature_set: FeatureSet,
    feature_provenance: FeatureArtifactProvenance,
    lineage_authority: DatasetSnapshotLineageAuthority,
    decision_cutoff_utc: str,
    feature_payload_sha256: str | None = None,
) -> FeatureAvailabilityEvidence:
    if type(dataset_snapshot) is not DatasetSnapshot:
        raise evidence.PointInTimeEvidenceError(
            "dataset_snapshot must be an exact DatasetSnapshot"
        )
    if type(feature_set) is not FeatureSet:
        raise evidence.PointInTimeEvidenceError(
            "feature_set must be an exact FeatureSet"
        )
    if type(feature_provenance) is not FeatureArtifactProvenance:
        raise evidence.PointInTimeEvidenceError(
            "feature_provenance must be an exact FeatureArtifactProvenance"
        )
    if not isinstance(lineage_authority, DatasetSnapshotLineageAuthority):
        raise evidence.PointInTimeEvidenceError(
            "lineage_authority must be a DatasetSnapshotLineageAuthority"
        )

    decision_cutoff = _instant(decision_cutoff_utc, "decision_cutoff_utc")
    dataset_cutoff = _instant(
        dataset_snapshot.causal_cutoff, "dataset_snapshot.causal_cutoff"
    )
    dataset_available = _instant(
        dataset_snapshot.available_at_utc, "dataset_snapshot.available_at"
    )
    feature_available = _instant(feature_set.available_at_utc, "feature_set.available_at")
    if dataset_cutoff > decision_cutoff:
        raise evidence.FutureEvidenceError("dataset causal cutoff is after decision cutoff")
    if dataset_available > decision_cutoff:
        raise evidence.FutureEvidenceError(
            "dataset snapshot was not available by decision cutoff"
        )
    if feature_available > decision_cutoff:
        raise evidence.FutureEvidenceError(
            "feature set was not available by decision cutoff"
        )

    _require_exact_registry_records(
        lineage_authority=lineage_authority,
        dataset_snapshot=dataset_snapshot,
        feature_set=feature_set,
    )
    _require_provenance_matches(
        feature_provenance,
        dataset_snapshot=dataset_snapshot,
        feature_set=feature_set,
    )

    try:
        lineage = lineage_authority.record(dataset_snapshot.dataset_snapshot_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise evidence.PointInTimeEvidenceError(
            "dataset lineage authority cannot resolve exact snapshot"
        ) from exc
    if lineage is None:
        raise evidence.PointInTimeEvidenceError(
            "dataset snapshot lacks canonical lineage authority"
        )
    if (
        lineage.manifest_sha256 != dataset_snapshot.manifest_sha256.lower()
        or lineage.source_identity != dataset_snapshot.source_identity
        or lineage.license_identity != dataset_snapshot.license_identity
    ):
        raise evidence.PointInTimeEvidenceError(
            "dataset lineage does not match exact canonical snapshot"
        )
    if feature_provenance.provenance_sha256 not in lineage.member_sha256:
        raise evidence.PointInTimeEvidenceError(
            "feature provenance is not committed by the dataset membership manifest"
        )
    if lineage.proof_registered_at is None:
        raise evidence.FutureEvidenceError(
            "feature provenance lacks authority-owned publication time"
        )
    lineage_published = _instant(
        lineage.proof_registered_at, "dataset_lineage.proof_registered_at"
    )
    if lineage_published > decision_cutoff:
        raise evidence.FutureEvidenceError(
            "feature provenance was not published by decision cutoff"
        )

    if feature_payload_sha256 is not None:
        asserted = _sha256(feature_payload_sha256, "feature_payload_sha256")
        if asserted != feature_provenance.feature_payload_sha256:
            raise evidence.PointInTimeEvidenceError(
                "feature payload audit digest does not match canonical provenance"
            )

    authoritative_available = max(
        dataset_available,
        feature_available,
        lineage_published,
    )
    return FeatureAvailabilityEvidence(
        feature_identity=feature_set.feature_set_id,
        feature_version=feature_set.version,
        feature_definition_sha256=feature_set.definition_sha256,
        source_identity=dataset_snapshot.source_identity,
        source_revision=feature_set.source_sha256,
        revision_policy_id=_REVISION_POLICY_ID,
        dataset_snapshot_id=dataset_snapshot.dataset_snapshot_id,
        dataset_record_sha256=lineage.dataset_record_sha256,
        dataset_manifest_sha256=dataset_snapshot.manifest_sha256,
        dataset_lineage_proof_sha256=lineage.proof_sha256,
        feature_provenance_sha256=feature_provenance.provenance_sha256,
        feature_payload_sha256=feature_provenance.feature_payload_sha256,
        as_of_utc=_utc(dataset_snapshot.causal_cutoff, "dataset_snapshot.causal_cutoff"),
        available_at_utc=authoritative_available.isoformat().replace("+00:00", "Z"),
        decision_cutoff_utc=_utc(decision_cutoff_utc, "decision_cutoff_utc"),
    )


# Replace only the feature-availability surface.  Holdout-consumption ownership and
# durable writer/recovery logic remain in point_in_time_evidence.py.
evidence.FeatureArtifactProvenance = FeatureArtifactProvenance
evidence.FeatureAvailabilityEvidence = FeatureAvailabilityEvidence
evidence.PointInTimeFeatureAuthority.bind = staticmethod(_bind)
