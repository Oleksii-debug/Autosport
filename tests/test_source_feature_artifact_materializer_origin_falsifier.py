from __future__ import annotations

from pathlib import Path

import pytest

from autosport import _dataset_snapshot_lineage_publication_trust_root as lineage_trust_root
from autosport import dataset_snapshot_lineage as lineage_module
from autosport import point_in_time_evidence as evidence
from autosport.causal_collector import CollectorDelta, CollectorDeltaStore
from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    membership_manifest_sha256,
)
from autosport.point_in_time_evidence import (
    FeatureArtifactProvenance,
    PointInTimeFeatureAuthority,
)
from autosport.scientific_registry import DatasetSnapshot, FeatureSet, ScientificRegistry
from autosport.source_feature_artifact_authority import (
    COLLECTOR_SOURCE_FEATURE_DEFINITION_SHA256,
    COLLECTOR_SOURCE_FEATURE_PRODUCER_SHA256,
    SourceFeatureArtifactMaterializer,
    collector_source_feature_payload,
)


def _authority_root(tmp_path: Path) -> Path:
    return tmp_path.parent / f"{tmp_path.name}-machine-authority"


def _canonical_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    DatasetSnapshot,
    FeatureSet,
    FeatureArtifactProvenance,
    DatasetSnapshotLineageAuthority,
    CollectorDeltaStore,
    CollectorDelta,
]:
    authority_root = _authority_root(tmp_path).resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: authority_root,
    )
    monkeypatch.setattr(
        lineage_module,
        "_authority_now_utc",
        lambda: "2026-09-20T10:01:45Z",
    )

    store = CollectorDeltaStore(tmp_path / "collector_deltas.json")
    delta = CollectorDelta(
        schema_version=1,
        delta_id="delta-caller-origin",
        source_id="source-caller-origin",
        lawful_terms_ref="terms:v1",
        retention_ref="retention:v1",
        stream_epoch="epoch-1",
        source_cursor="cursor-1",
        cursor_position=1,
        event_dedupe_key="event-key-1",
        event_id="event-1",
        source_payload_digest="a" * 64,
        canonical_event_digest="b" * 64,
        source_observed_at="2026-09-20T09:58:00Z",
        collector_received_at="2026-09-20T09:58:30Z",
        collector_committed_at="2026-09-20T09:59:00Z",
        desktop_available_at="2026-09-20T09:59:30Z",
    )
    assert store.append(delta) is True

    feature_set = FeatureSet(
        feature_set_id="collector.source-evidence.caller-origin.v1",
        version="v1",
        definition_sha256=COLLECTOR_SOURCE_FEATURE_DEFINITION_SHA256,
        source_sha256=COLLECTOR_SOURCE_FEATURE_PRODUCER_SHA256,
        available_at_utc="2026-09-20T10:01:30Z",
    )
    provisional = DatasetSnapshot(
        dataset_snapshot_id="snapshot-caller-origin",
        manifest_sha256="c" * 64,
        source_identity=delta.source_id,
        license_identity="terms:v1",
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )
    payload = collector_source_feature_payload(
        source_delta=delta,
        dataset_snapshot=provisional,
        feature_set=feature_set,
    )
    provenance = FeatureArtifactProvenance.issue(
        dataset_snapshot=provisional,
        feature_set=feature_set,
        feature_payload=payload,
    )
    snapshot = DatasetSnapshot(
        dataset_snapshot_id=provisional.dataset_snapshot_id,
        manifest_sha256=membership_manifest_sha256(
            (provenance.provenance_sha256,)
        ),
        source_identity=provisional.source_identity,
        license_identity=provisional.license_identity,
        causal_cutoff=provisional.causal_cutoff,
        available_at_utc=provisional.available_at_utc,
    )

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    registry.append(snapshot)
    registry.append(feature_set)
    lineage = DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry,
        authority_root=authority_root,
    )
    lineage.register(
        snapshot_id=snapshot.dataset_snapshot_id,
        member_sha256=(provenance.provenance_sha256,),
    )
    return snapshot, feature_set, provenance, lineage, store, delta


def test_ordinary_caller_cannot_mint_source_publication_via_public_materializer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical values are not proof that this call originated in ingestion.

    An evaluator/application caller can resolve the exact canonical lineage and
    CollectorDeltaStore too. Constructing the public materializer must therefore
    not be sufficient to create the first-publication fact consumed by the
    point-in-time authority.
    """

    snapshot, feature_set, provenance, lineage, store, delta = _canonical_context(
        tmp_path, monkeypatch
    )

    caller_constructed_materializer = SourceFeatureArtifactMaterializer(
        lineage,
        collector_store=store,
    )
    caller_constructed_materializer.materialize(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        source_delta_id=delta.delta_id,
    )

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="source|ingestion|issuance|publication",
    ):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot,
            feature_set=feature_set,
            feature_provenance=provenance,
            lineage_authority=lineage,
            feature_artifact_authority=caller_constructed_materializer.authority,
            decision_cutoff_utc="2100-01-01T00:00:00Z",
        )
