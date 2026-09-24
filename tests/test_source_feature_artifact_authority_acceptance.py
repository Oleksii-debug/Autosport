from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from autosport import _dataset_snapshot_lineage_publication_trust_root as lineage_trust_root
from autosport import dataset_snapshot_lineage as lineage_module
from autosport import point_in_time_evidence as evidence
from autosport.causal_collector import CollectorDelta, CollectorDeltaStore
from autosport.collector_service import HeadlessCollectorService
from autosport.event_lifecycle import ContinuousEventLifecycle
from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    membership_manifest_sha256,
)
from autosport.point_in_time_evidence import (
    FeatureArtifactProvenance,
    FutureEvidenceError,
    PointInTimeFeatureAuthority,
)
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
    monkeypatch.setattr(
        lineage_module,
        "_authority_now_utc",
        lambda: "2026-09-20T10:01:45Z",
    )


def _delta() -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id="delta-1",
        source_id="source-1",
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


def _feature() -> FeatureSet:
    return FeatureSet(
        feature_set_id="collector.source-evidence.v1",
        version="v1",
        definition_sha256=COLLECTOR_SOURCE_FEATURE_DEFINITION_SHA256,
        source_sha256=COLLECTOR_SOURCE_FEATURE_PRODUCER_SHA256,
        available_at_utc="2026-09-20T10:01:30Z",
    )


def _context(tmp_path: Path):
    store = CollectorDeltaStore(tmp_path / "collector_deltas.json")
    delta = _delta()
    assert store.append(delta) is True
    feature_set = _feature()
    provisional = DatasetSnapshot(
        dataset_snapshot_id="snapshot-a",
        manifest_sha256="a" * 64,
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
    members = (provenance.provenance_sha256,)
    snapshot = DatasetSnapshot(
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
    registry.append(snapshot)
    registry.append(feature_set)
    lineage = DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry,
        authority_root=_authority_root(tmp_path),
    )
    lineage.register(
        snapshot_id=snapshot.dataset_snapshot_id,
        member_sha256=members,
    )
    return snapshot, feature_set, provenance, lineage, store, delta, payload


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

def _append_child_snapshot(
    *,
    lineage: DatasetSnapshotLineageAuthority,
    parent: DatasetSnapshot,
    feature_set: FeatureSet,
    source_delta: CollectorDelta,
):
    provisional = DatasetSnapshot(
        dataset_snapshot_id="snapshot-b",
        manifest_sha256="b" * 64,
        source_identity=parent.source_identity,
        license_identity=parent.license_identity,
        causal_cutoff="2026-09-20T10:01:20Z",
        available_at_utc="2026-09-20T10:01:20Z",
    )
    payload = collector_source_feature_payload(
        source_delta=source_delta,
        dataset_snapshot=provisional,
        feature_set=feature_set,
    )
    provenance = FeatureArtifactProvenance.issue(
        dataset_snapshot=provisional,
        feature_set=feature_set,
        feature_payload=payload,
    )
    parent_record = lineage.record(parent.dataset_snapshot_id)
    assert parent_record is not None
    members = (*parent_record.member_sha256, provenance.provenance_sha256)
    snapshot = DatasetSnapshot(
        dataset_snapshot_id=provisional.dataset_snapshot_id,
        manifest_sha256=membership_manifest_sha256(members),
        source_identity=provisional.source_identity,
        license_identity=provisional.license_identity,
        causal_cutoff=provisional.causal_cutoff,
        available_at_utc=provisional.available_at_utc,
    )
    lineage.registry.append(snapshot)
    lineage.register(
        snapshot_id=snapshot.dataset_snapshot_id,
        member_sha256=members,
        parent_snapshot_id=parent.dataset_snapshot_id,
    )
    return snapshot, provenance, payload


def test_concurrent_first_publication_is_single_and_idempotent(tmp_path: Path) -> None:
    snapshot, feature_set, _, lineage, store, delta, payload = _context(tmp_path)
    first_client = _materializer(lineage, store)
    second_client = _materializer(lineage, store)
    start = threading.Barrier(3)
    records = []
    failures = []

    def publish(materializer: SourceFeatureArtifactMaterializer) -> None:
        try:
            start.wait()
            records.append(
                materializer.materialize(
                    dataset_snapshot=snapshot,
                    feature_set=feature_set,
                    source_delta_id=delta.delta_id,
                )
            )
        except BaseException as exc:  # pragma: no cover
            failures.append(exc)

    threads = [
        threading.Thread(target=publish, args=(first_client,)),
        threading.Thread(target=publish, args=(second_client,)),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(records) == 2
    assert records[0] == records[1]
    assert records[0].feature_payload_sha256 == hashlib.sha256(payload).hexdigest()


def test_positive_binding_consumes_exact_source_publication(tmp_path: Path) -> None:
    snapshot, feature_set, provenance, lineage, store, delta, _ = _context(tmp_path)
    materializer = _materializer(lineage, store)
    materializer.materialize(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        source_delta_id=delta.delta_id,
    )

    bound = PointInTimeFeatureAuthority.bind(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_provenance=provenance,
        lineage_authority=lineage,
        feature_artifact_authority=materializer.authority,
        decision_cutoff_utc="2100-01-01T00:00:00Z",
    )

    assert bound.feature_payload_sha256 == provenance.feature_payload_sha256
    assert datetime.fromisoformat(
        bound.available_at_utc.replace("Z", "+00:00")
    ) >= datetime.fromisoformat(
        delta.desktop_available_at.replace("Z", "+00:00")
    )


def test_post_hoc_publication_cannot_become_historically_available(tmp_path: Path) -> None:
    snapshot, feature_set, provenance, lineage, store, delta, _ = _context(tmp_path)
    materializer = _materializer(lineage, store)
    materializer.materialize(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        source_delta_id=delta.delta_id,
    )

    with pytest.raises(
        FutureEvidenceError,
        match="not source-published by decision cutoff",
    ):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot,
            feature_set=feature_set,
            feature_provenance=provenance,
            lineage_authority=lineage,
            feature_artifact_authority=materializer.authority,
            decision_cutoff_utc="2026-09-20T10:03:00Z",
        )


def test_exact_dtos_and_historical_bytes_cannot_mint_without_source_path(
    tmp_path: Path,
) -> None:
    snapshot, feature_set, provenance, lineage, store, delta, payload = _context(tmp_path)
    materializer = _materializer(lineage, store)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="caller-supplied feature_payload is forbidden",
    ):
        materializer.materialize(
            dataset_snapshot=snapshot,
            feature_set=feature_set,
            source_delta_id=delta.delta_id,
            feature_payload=payload,
        )

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="requires an independent source-owned feature artifact authority",
    ):
        materializer.authority.resolve(
            dataset_snapshot=snapshot,
            feature_set=feature_set,
            feature_provenance=provenance,
        )


def test_module_global_clock_rebinding_cannot_backdate_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autosport.source_feature_artifact_authority as module

    snapshot, feature_set, _, lineage, store, delta, _ = _context(tmp_path)
    monkeypatch.setattr(module, "_utc_now", lambda: "2000-01-01T00:00:00Z")
    record = _materializer(lineage, store).materialize(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        source_delta_id=delta.delta_id,
    )

    assert record.first_published_at_utc != "2000-01-01T00:00:00Z"
    published = datetime.fromisoformat(
        record.first_published_at_utc.replace("Z", "+00:00")
    )
    source_available = datetime.fromisoformat(
        delta.desktop_available_at.replace("Z", "+00:00")
    )
    assert published >= source_available
    assert published <= datetime.now(timezone.utc)


def test_valid_old_whole_file_rollback_after_second_publication_is_rejected(
    tmp_path: Path,
) -> None:
    snapshot_a, feature_set, _, lineage, store, delta, _ = _context(tmp_path)
    materializer = _materializer(lineage, store)
    materializer.materialize(
        dataset_snapshot=snapshot_a,
        feature_set=feature_set,
        source_delta_id=delta.delta_id,
    )
    path = materializer.authority.path
    post_a = path.read_bytes()

    snapshot_b, _, _ = _append_child_snapshot(
        lineage=lineage,
        parent=snapshot_a,
        feature_set=feature_set,
        source_delta=delta,
    )
    materializer.materialize(
        dataset_snapshot=snapshot_b,
        feature_set=feature_set,
        source_delta_id=delta.delta_id,
    )
    assert path.read_bytes() != post_a

    path.write_bytes(post_a)
    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="monotonic history rejected workspace state",
    ):
        SourceFeatureArtifactAuthority.for_lineage(lineage)


def test_committed_publication_deletion_is_rejected_on_reopen(tmp_path: Path) -> None:
    snapshot, feature_set, _, lineage, store, delta, _ = _context(tmp_path)
    materializer = _materializer(lineage, store)
    materializer.materialize(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        source_delta_id=delta.delta_id,
    )
    materializer.authority.path.unlink()

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="deleted or rolled back",
    ):
        SourceFeatureArtifactAuthority.for_lineage(lineage)


def test_crash_after_prepare_before_local_publish_aborts_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autosport.source_feature_artifact_authority as module

    snapshot, feature_set, provenance, lineage, store, delta, _ = _context(tmp_path)
    materializer = _materializer(lineage, store)
    original_write = module.atomic_write_json

    def crash_before_publish(*_args, **_kwargs):
        raise RuntimeError("simulated crash before local publish")

    monkeypatch.setattr(module, "atomic_write_json", crash_before_publish)
    with pytest.raises(RuntimeError, match="simulated crash"):
        materializer.materialize(
            dataset_snapshot=snapshot,
            feature_set=feature_set,
            source_delta_id=delta.delta_id,
        )
    monkeypatch.setattr(module, "atomic_write_json", original_write)

    recovered = _materializer(lineage, store)
    record = recovered.materialize(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        source_delta_id=delta.delta_id,
    )
    assert recovered.authority.resolve(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_provenance=provenance,
    ) == record


def test_crash_after_local_publish_before_commit_self_commits_on_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, feature_set, provenance, lineage, store, delta, _ = _context(tmp_path)
    materializer = _materializer(lineage, store)

    def crash_before_commit(**_kwargs):
        raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(
        materializer.authority.monotonic_authority,
        "commit",
        crash_before_commit,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        materializer.materialize(
            dataset_snapshot=snapshot,
            feature_set=feature_set,
            source_delta_id=delta.delta_id,
        )

    recovered = SourceFeatureArtifactAuthority.for_lineage(lineage)
    record = recovered.resolve(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_provenance=provenance,
    )
    assert record.feature_payload_sha256 == provenance.feature_payload_sha256


def test_direct_authority_mint_cannot_create_positive_availability(tmp_path: Path) -> None:
    snapshot, feature_set, provenance, lineage, _, _, payload = _context(tmp_path)
    authority = SourceFeatureArtifactAuthority.for_lineage(lineage)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="direct feature publication is forbidden",
    ):
        authority.publish(
            dataset_snapshot=snapshot,
            feature_set=feature_set,
            feature_payload=payload,
        )
    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="requires an independent source-owned feature artifact authority",
    ):
        authority.resolve(
            dataset_snapshot=snapshot,
            feature_set=feature_set,
            feature_provenance=provenance,
        )
