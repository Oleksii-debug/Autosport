from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from autosport import point_in_time_evidence as evidence
from autosport.scientific_registry import DatasetSnapshot, FeatureSet
from autosport.source_feature_artifact_authority import SourceFeatureArtifactAuthority


def _dataset() -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset_snapshot_id="dataset-1",
        manifest_sha256="1" * 64,
        source_identity="source-1",
        license_identity="license-1",
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:05:00Z",
    )


def _feature() -> FeatureSet:
    return FeatureSet(
        feature_set_id="features-1",
        version="1",
        definition_sha256="2" * 64,
        source_sha256="3" * 64,
        available_at_utc="2026-09-20T10:06:00Z",
    )


def test_first_publication_is_restart_stable_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    import autosport.source_feature_artifact_authority as module

    observed = iter(("2026-09-20T10:07:00Z", "2026-09-20T11:00:00Z"))
    monkeypatch.setattr(module, "_utc_now", lambda: next(observed))
    authority = SourceFeatureArtifactAuthority.for_workspace(tmp_path)
    payload = b"canonical-feature-payload"

    first = authority.publish(
        dataset_snapshot=_dataset(),
        feature_set=_feature(),
        feature_payload=payload,
    )
    restarted = SourceFeatureArtifactAuthority.for_workspace(tmp_path)
    second = restarted.publish(
        dataset_snapshot=_dataset(),
        feature_set=_feature(),
        feature_payload=payload,
    )

    assert second == first
    assert first.first_published_at_utc == "2026-09-20T10:07:00Z"
    assert first.feature_payload_sha256 == hashlib.sha256(payload).hexdigest()


def test_same_dataset_feature_identity_cannot_rebind_payload(tmp_path: Path) -> None:
    authority = SourceFeatureArtifactAuthority.for_workspace(tmp_path)
    authority.publish(
        dataset_snapshot=_dataset(),
        feature_set=_feature(),
        feature_payload=b"first",
    )

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="publication identity cannot be rebound",
    ):
        authority.publish(
            dataset_snapshot=_dataset(),
            feature_set=_feature(),
            feature_payload=b"different",
        )


def test_resolve_requires_exact_source_owned_payload(tmp_path: Path) -> None:
    authority = SourceFeatureArtifactAuthority.for_workspace(tmp_path)
    payload = b"canonical-feature-payload"
    authority.publish(
        dataset_snapshot=_dataset(),
        feature_set=_feature(),
        feature_payload=payload,
    )

    exact = authority.resolve(
        dataset_snapshot=_dataset(),
        feature_set=_feature(),
        feature_payload_sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert exact.feature_set_id == "features-1"

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="does not match exact feature",
    ):
        authority.resolve(
            dataset_snapshot=_dataset(),
            feature_set=_feature(),
            feature_payload_sha256="4" * 64,
        )


def test_missing_publication_fails_closed(tmp_path: Path) -> None:
    authority = SourceFeatureArtifactAuthority.for_workspace(tmp_path)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="requires an independent source-owned feature artifact authority",
    ):
        authority.resolve(
            dataset_snapshot=_dataset(),
            feature_set=_feature(),
            feature_payload_sha256="4" * 64,
        )


def test_state_digest_tamper_fails_on_restart(tmp_path: Path) -> None:
    authority = SourceFeatureArtifactAuthority.for_workspace(tmp_path)
    authority.publish(
        dataset_snapshot=_dataset(),
        feature_set=_feature(),
        feature_payload=b"canonical-feature-payload",
    )
    path = tmp_path / "source-feature-artifact-publications.json"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("source-1", "source-X"), encoding="utf-8")

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="state digest mismatch",
    ):
        SourceFeatureArtifactAuthority.for_workspace(tmp_path)
