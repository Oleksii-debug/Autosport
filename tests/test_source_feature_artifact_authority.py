from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from autosport import _dataset_snapshot_lineage_publication_trust_root as lineage_trust_root
from autosport import point_in_time_evidence as evidence
from autosport.causal_collector import CollectorDelta, CollectorDeltaStore
from autosport.collector_service import HeadlessCollectorService
from autosport.event_lifecycle import ContinuousEventLifecycle
from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    membership_manifest_sha256,
)
from autosport.point_in_time_evidence import FeatureArtifactProvenance
from autosport.scientific_registry import DatasetSnapshot, FeatureSet, ScientificRegistry
from autosport.source_feature_artifact_authority import (
    COLLECTOR_SOURCE_FEATURE_DEFINITION_SHA256,
    COLLECTOR_SOURCE_FEATURE_PRODUCER_SHA256,
    SourceFeatureArtifactAuthority,
    SourceFeatureArtifactMaterializer,
    collector_source_feature_payload,
)


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


def _delta(*, delta_id: str = "delta-1", digest: str = "b" * 64) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="source-1",
        lawful_terms_ref="terms:v1",
        retention_ref="retention:v1",
        stream_epoch="epoch-1",
        source_cursor="cursor-1",
        cursor_position=1,
        event_dedupe_key="event-key-1",
        event_id="event-1",
        source_payload_digest="a" * 64,
        canonical_event_digest=digest,
        source_observed_at="2026-09-20T09:58:00Z",
        collector_received_at="2026-09-20T09:58:30Z",
        collector_committed_at="2026-09-20T09:59:00Z",
        desktop_available_at="2026-09-20T09:59:30Z",
    )


def _context(tmp_path: Path):
    store = CollectorDeltaStore(tmp_path / "collector_deltas.json")
    delta = _delta()
    assert store.append(delta) is True

    feature = FeatureSet(
        feature_set_id="collector.source-evidence.v1",
        version="v1",
        definition_sha256=COLLECTOR_SOURCE_FEATURE_DEFINITION_SHA256,
        source_sha256=COLLECTOR_SOURCE_FEATURE_PRODUCER_SHA256,
        available_at_utc="2026-09-20T10:01:30Z",
    )
    provisional = DatasetSnapshot(
        dataset_snapshot_id="dataset-1",
        manifest_sha256="1" * 64,
        source_identity="source-1",
        license_identity="license-1",
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )
    payload = collector_source_feature_payload(
        source_delta=delta,
        dataset_snapshot=provisional,
        feature_set=feature,
    )
    provenance = FeatureArtifactProvenance.issue(
        dataset_snapshot=provisional,
        feature_set=feature,
        feature_payload=payload,
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
    return dataset, feature, provenance, lineage, store, delta, payload


class _FeatureSource:
    def __init__(self, source_id: str, stream_epoch: str) -> None:
        self.source_id = source_id
        self.stream_epoch = stream_epoch

    def fetch_catalog_page(self, *_args, **_kwargs):
        raise AssertionError("feature materialization test must not fetch provider catalog")

    def fetch_deltas(self, *_args, **_kwargs):
        raise AssertionError("feature materialization test must not fetch provider deltas")


def _materializer(
    lineage: DatasetSnapshotLineageAuthority,
    store: CollectorDeltaStore,
) -> SourceFeatureArtifactMaterializer:
    deltas = store._all()
    assert deltas
    source = _FeatureSource(deltas[0].source_id, deltas[0].stream_epoch)
    service = HeadlessCollectorService(
        delta_store=store,
        lifecycle=ContinuousEventLifecycle(
            lineage.path.with_name("source-feature-test-catalog.json")
        ),
        source=source,
        state_path=lineage.path.with_name("source-feature-test-service-state.json"),
        run_id="source-feature-materialization-test",
        clock=lambda: "2026-09-20T10:04:00Z",
    )
    return service.source_feature_materializer(lineage_authority=lineage)

def test_first_publication_is_restart_stable_and_idempotent(tmp_path: Path) -> None:
    dataset, feature, _, lineage, store, delta, payload = _context(tmp_path)
    first = _materializer(lineage, store).materialize(
        dataset_snapshot=dataset,
        feature_set=feature,
        source_delta_id=delta.delta_id,
    )
    second = _materializer(lineage, store).materialize(
        dataset_snapshot=dataset,
        feature_set=feature,
        source_delta_id=delta.delta_id,
    )

    assert second == first
    assert first.feature_payload_sha256 == hashlib.sha256(payload).hexdigest()


def test_ordinary_caller_cannot_mint_via_direct_materializer(tmp_path: Path) -> None:
    dataset, feature, provenance, lineage, store, delta, _ = _context(tmp_path)
    caller_materializer = SourceFeatureArtifactMaterializer(
        lineage,
        collector_store=store,
    )

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="HeadlessCollectorService|source runtime",
    ):
        caller_materializer.materialize(
            dataset_snapshot=dataset,
            feature_set=feature,
            source_delta_id=delta.delta_id,
        )

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="requires an independent source-owned feature artifact authority",
    ):
        evidence.PointInTimeFeatureAuthority.bind(
            dataset_snapshot=dataset,
            feature_set=feature,
            feature_provenance=provenance,
            lineage_authority=lineage,
            feature_artifact_authority=caller_materializer.authority,
            decision_cutoff_utc="2100-01-01T00:00:00Z",
        )


def test_direct_evaluator_publication_is_forbidden(tmp_path: Path) -> None:
    dataset, feature, _, lineage, _, _, payload = _context(tmp_path)
    authority = SourceFeatureArtifactAuthority.for_lineage(lineage)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="direct feature publication is forbidden",
    ):
        authority.publish(
            dataset_snapshot=dataset,
            feature_set=feature,
            feature_payload=payload,
        )


def test_exact_dtos_and_feature_bytes_cannot_self_issue_publication(
    tmp_path: Path,
) -> None:
    dataset, feature, _, lineage, store, delta, payload = _context(tmp_path)
    materializer = _materializer(lineage, store)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="caller-supplied feature_payload is forbidden",
    ):
        materializer.materialize(
            dataset_snapshot=dataset,
            feature_set=feature,
            source_delta_id=delta.delta_id,
            feature_payload=payload,
        )

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="requires an independent source-owned feature artifact authority",
    ):
        materializer.authority.resolve(
            dataset_snapshot=dataset,
            feature_set=feature,
            feature_provenance=FeatureArtifactProvenance.issue(
                dataset_snapshot=dataset,
                feature_set=feature,
                feature_payload=payload,
            ),
        )


def test_source_delta_is_re_resolved_from_canonical_store(tmp_path: Path) -> None:
    dataset, feature, _, lineage, store, _, _ = _context(tmp_path)
    materializer = _materializer(lineage, store)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="source_delta_id is not present",
    ):
        materializer.materialize(
            dataset_snapshot=dataset,
            feature_set=feature,
            source_delta_id="missing-delta",
        )


def test_noncanonical_collector_store_is_rejected(tmp_path: Path) -> None:
    _, _, _, lineage, _, _, _ = _context(tmp_path)
    other = CollectorDeltaStore(tmp_path / "other-collector.json")

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="exact lineage workspace",
    ):
        SourceFeatureArtifactMaterializer(
            lineage,
            collector_store=other,
        )


def test_resolve_requires_exact_source_owned_provenance(tmp_path: Path) -> None:
    dataset, feature, provenance, lineage, store, delta, _ = _context(tmp_path)
    materializer = _materializer(lineage, store)
    materializer.materialize(
        dataset_snapshot=dataset,
        feature_set=feature,
        source_delta_id=delta.delta_id,
    )

    exact = materializer.authority.resolve(
        dataset_snapshot=dataset,
        feature_set=feature,
        feature_provenance=provenance,
    )
    assert exact.feature_set_id == feature.feature_set_id

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
    dataset, feature, provenance, lineage, _, _, _ = _context(tmp_path)
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
    dataset, feature, _, lineage, store, delta, _ = _context(tmp_path)
    materializer = _materializer(lineage, store)
    materializer.materialize(
        dataset_snapshot=dataset,
        feature_set=feature,
        source_delta_id=delta.delta_id,
    )
    path = tmp_path / "source-feature-artifact-publications.json"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("source-1", "source-X"), encoding="utf-8")

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="record digest mismatch|state digest mismatch",
    ):
        SourceFeatureArtifactAuthority.for_lineage(lineage)


def test_source_identity_and_causal_cutoff_are_source_owned_inputs(tmp_path: Path) -> None:
    _, feature, _, _, _, delta, _ = _context(tmp_path)
    wrong_source = DatasetSnapshot(
        dataset_snapshot_id="wrong-source",
        manifest_sha256="1" * 64,
        source_identity="other-source",
        license_identity="license-1",
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )
    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="source identity",
    ):
        collector_source_feature_payload(
            source_delta=delta,
            dataset_snapshot=wrong_source,
            feature_set=feature,
        )

    too_early = DatasetSnapshot(
        dataset_snapshot_id="too-early",
        manifest_sha256="1" * 64,
        source_identity=delta.source_id,
        license_identity="license-1",
        causal_cutoff="2026-09-20T09:58:45Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )
    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="after DatasetSnapshot causal cutoff",
    ):
        collector_source_feature_payload(
            source_delta=delta,
            dataset_snapshot=too_early,
            feature_set=feature,
        )


def test_feature_definition_must_be_product_producer_contract(tmp_path: Path) -> None:
    dataset, _, _, _, _, delta, _ = _context(tmp_path)
    forged = FeatureSet(
        feature_set_id="forged",
        version="v1",
        definition_sha256="e" * 64,
        source_sha256="f" * 64,
        available_at_utc="2026-09-20T10:01:30Z",
    )

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="canonical collector source-feature producer",
    ):
        collector_source_feature_payload(
            source_delta=delta,
            dataset_snapshot=dataset,
            feature_set=forged,
        )
