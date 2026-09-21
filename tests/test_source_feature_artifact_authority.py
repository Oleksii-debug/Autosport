from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from autosport import _dataset_snapshot_lineage_publication_trust_root as lineage_trust_root
from autosport import point_in_time_evidence as evidence
from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    membership_manifest_sha256,
)
from autosport.point_in_time_evidence import FeatureArtifactProvenance
from autosport.scientific_registry import DatasetSnapshot, FeatureSet, ScientificRegistry
from autosport.source_feature_artifact_authority import (
    SourceFeatureArtifactAuthority,
    SourceFeatureArtifactMaterializer,
)


_PAYLOAD = b"canonical-feature-payload"


def _authority_root(tmp_path: Path) -> Path:
    return tmp_path.parent / f"{tmp_path.name}-machine-authority"


@pytest.fixture(autouse=True)
def _use_isolated_product_authority_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_root = _authority_root(tmp_path).resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: product_root,
    )


def _context(tmp_path: Path):
    feature = FeatureSet(
        feature_set_id="features-1",
        version="1",
        definition_sha256="2" * 64,
        source_sha256="3" * 64,
        available_at_utc="2026-09-20T10:06:00Z",
    )
    provisional = DatasetSnapshot(
        dataset_snapshot_id="dataset-1",
        manifest_sha256="1" * 64,
        source_identity="source-1",
        license_identity="license-1",
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:05:00Z",
    )
    provenance = FeatureArtifactProvenance.issue(
        dataset_snapshot=provisional,
        feature_set=feature,
        feature_payload=_PAYLOAD,
    )
    members = (provenance.provenance_sha256,)
    dataset = DatasetSnapshot(
        dataset_snapshot_id=provisional.dataset_snapshot_id,
        manifest_sha256=membership_manifest_sha256(members),
        source_identity=provisional.source_identity,
        license_identity=provisional.license_identity,
        causal_cutoff=provisional.causal_cutoff,
        available_at_utc=provisional.available_at_utc,
    )
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    registry.append(dataset)
    registry.append(feature)
    lineage = DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry,
        authority_root=_authority_root(tmp_path),
    )
    lineage.register(
        snapshot_id=dataset.dataset_snapshot_id,
        member_sha256=members,
    )
    return dataset, feature, provenance, lineage


def test_first_publication_is_restart_stable_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autosport.source_feature_artifact_authority as module

    observed = iter(("2026-09-20T10:07:00Z", "2026-09-20T11:00:00Z"))
    monkeypatch.setattr(module, "_utc_now", lambda: next(observed))
    dataset, feature, _, lineage = _context(tmp_path)
    materializer = SourceFeatureArtifactMaterializer(lineage)

    first = materializer.materialize(
        dataset_snapshot=dataset,
        feature_set=feature,
        feature_payload=_PAYLOAD,
    )
    restarted = SourceFeatureArtifactMaterializer(lineage)
    second = restarted.materialize(
        dataset_snapshot=dataset,
        feature_set=feature,
        feature_payload=_PAYLOAD,
    )

    assert second == first
    assert first.first_published_at_utc == "2026-09-20T10:07:00Z"
    assert first.feature_payload_sha256 == hashlib.sha256(_PAYLOAD).hexdigest()


def test_direct_evaluator_publication_is_forbidden(tmp_path: Path) -> None:
    dataset, feature, _, lineage = _context(tmp_path)
    authority = SourceFeatureArtifactAuthority.for_lineage(lineage)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="direct feature publication is forbidden",
    ):
        authority.publish(
            dataset_snapshot=dataset,
            feature_set=feature,
            feature_payload=_PAYLOAD,
        )


def test_same_dataset_feature_identity_cannot_rebind_payload(tmp_path: Path) -> None:
    dataset, feature, _, lineage = _context(tmp_path)
    materializer = SourceFeatureArtifactMaterializer(lineage)
    materializer.materialize(
        dataset_snapshot=dataset,
        feature_set=feature,
        feature_payload=_PAYLOAD,
    )

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="provenance is not a member of canonical dataset lineage",
    ):
        materializer.materialize(
            dataset_snapshot=dataset,
            feature_set=feature,
            feature_payload=b"different",
        )


def test_resolve_requires_exact_source_owned_provenance(tmp_path: Path) -> None:
    dataset, feature, provenance, lineage = _context(tmp_path)
    materializer = SourceFeatureArtifactMaterializer(lineage)
    materializer.materialize(
        dataset_snapshot=dataset,
        feature_set=feature,
        feature_payload=_PAYLOAD,
    )

    exact = materializer.authority.resolve(
        dataset_snapshot=dataset,
        feature_set=feature,
        feature_provenance=provenance,
    )
    assert exact.feature_set_id == "features-1"

    substituted = FeatureArtifactProvenance.issue(
        dataset_snapshot=dataset,
        feature_set=feature,
        feature_payload=b"different",
    )
    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="provenance is not a member of canonical dataset lineage",
    ):
        materializer.authority.resolve(
            dataset_snapshot=dataset,
            feature_set=feature,
            feature_provenance=substituted,
        )


def test_missing_publication_fails_closed(tmp_path: Path) -> None:
    dataset, feature, provenance, lineage = _context(tmp_path)
    authority = SourceFeatureArtifactAuthority.for_lineage(lineage)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="requires an independent source-owned feature artifact authority",
    ):
        authority.resolve(
            dataset_snapshot=dataset,
            feature_set=feature,
            feature_provenance=provenance,
        )


def test_local_state_digest_tamper_fails_on_restart(tmp_path: Path) -> None:
    dataset, feature, _, lineage = _context(tmp_path)
    materializer = SourceFeatureArtifactMaterializer(lineage)
    materializer.materialize(
        dataset_snapshot=dataset,
        feature_set=feature,
        feature_payload=_PAYLOAD,
    )
    path = tmp_path / "source-feature-artifact-publications.json"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("source-1", "source-X"), encoding="utf-8")

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="record digest mismatch|state digest mismatch",
    ):
        SourceFeatureArtifactAuthority.for_lineage(lineage)
