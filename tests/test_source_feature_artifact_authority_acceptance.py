from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from autosport import _dataset_snapshot_lineage_publication_trust_root as lineage_trust_root
from autosport import dataset_snapshot_lineage as lineage_module
from autosport import point_in_time_evidence as evidence
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
    SourceFeatureArtifactAuthority,
    SourceFeatureArtifactMaterializer,
)


_PAYLOAD = b"canonical-feature-payload-v1"
_SHA_C = "c" * 64
_SHA_D = "d" * 64


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


def _context(tmp_path: Path):
    feature_set = FeatureSet(
        feature_set_id="participant.form.mean.v1",
        version="v17",
        definition_sha256=_SHA_C,
        source_sha256=_SHA_D,
        available_at_utc="2026-09-20T10:01:30Z",
    )
    provisional_snapshot = DatasetSnapshot(
        dataset_snapshot_id="snapshot-a",
        manifest_sha256="a" * 64,
        source_identity="provider:canonical-feed",
        license_identity="terms:v1",
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )
    provenance = FeatureArtifactProvenance.issue(
        dataset_snapshot=provisional_snapshot,
        feature_set=feature_set,
        feature_payload=_PAYLOAD,
    )
    members = (provenance.provenance_sha256,)
    snapshot = DatasetSnapshot(
        dataset_snapshot_id=provisional_snapshot.dataset_snapshot_id,
        manifest_sha256=membership_manifest_sha256(members),
        source_identity=provisional_snapshot.source_identity,
        license_identity=provisional_snapshot.license_identity,
        causal_cutoff=provisional_snapshot.causal_cutoff,
        available_at_utc=provisional_snapshot.available_at_utc,
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
    return snapshot, feature_set, provenance, lineage


def _append_child_snapshot(
    *,
    lineage: DatasetSnapshotLineageAuthority,
    parent: DatasetSnapshot,
    feature_set: FeatureSet,
):
    provisional = DatasetSnapshot(
        dataset_snapshot_id="snapshot-b",
        manifest_sha256="b" * 64,
        source_identity=parent.source_identity,
        license_identity=parent.license_identity,
        causal_cutoff="2026-09-20T10:01:20Z",
        available_at_utc="2026-09-20T10:01:20Z",
    )
    provenance = FeatureArtifactProvenance.issue(
        dataset_snapshot=provisional,
        feature_set=feature_set,
        feature_payload=_PAYLOAD,
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
    return snapshot, provenance


def test_concurrent_first_publication_is_single_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autosport.source_feature_artifact_authority as module

    monkeypatch.setattr(module, "_utc_now", lambda: "2026-09-20T10:02:00Z")
    snapshot, feature_set, _, lineage = _context(tmp_path)
    first_client = SourceFeatureArtifactMaterializer(lineage)
    second_client = SourceFeatureArtifactMaterializer(lineage)
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
                    feature_payload=_PAYLOAD,
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports exact failure
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
    assert records[0].first_published_at_utc == "2026-09-20T10:02:00Z"
    assert records[0].feature_payload_sha256 == hashlib.sha256(_PAYLOAD).hexdigest()


def test_positive_binding_consumes_exact_source_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autosport.source_feature_artifact_authority as module

    monkeypatch.setattr(module, "_utc_now", lambda: "2026-09-20T10:02:00Z")
    snapshot, feature_set, provenance, lineage = _context(tmp_path)
    materializer = SourceFeatureArtifactMaterializer(lineage)
    materializer.materialize(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_payload=_PAYLOAD,
    )

    bound = PointInTimeFeatureAuthority.bind(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_provenance=provenance,
        lineage_authority=lineage,
        feature_artifact_authority=materializer.authority,
        decision_cutoff_utc="2026-09-20T10:03:00Z",
    )

    assert bound.feature_payload_sha256 == hashlib.sha256(_PAYLOAD).hexdigest()
    assert bound.available_at_utc == "2026-09-20T10:02:00Z"


def test_post_hoc_publication_cannot_become_historically_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autosport.source_feature_artifact_authority as module

    monkeypatch.setattr(module, "_utc_now", lambda: "2026-09-20T10:04:00Z")
    snapshot, feature_set, provenance, lineage = _context(tmp_path)
    materializer = SourceFeatureArtifactMaterializer(lineage)
    materializer.materialize(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_payload=_PAYLOAD,
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


def test_valid_old_whole_file_rollback_after_second_publication_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autosport.source_feature_artifact_authority as module

    observed = iter(("2026-09-20T10:02:00Z", "2026-09-20T10:02:30Z"))
    monkeypatch.setattr(module, "_utc_now", lambda: next(observed))
    snapshot_a, feature_set, _, lineage = _context(tmp_path)
    materializer = SourceFeatureArtifactMaterializer(lineage)
    materializer.materialize(
        dataset_snapshot=snapshot_a,
        feature_set=feature_set,
        feature_payload=_PAYLOAD,
    )
    path = materializer.authority.path
    post_a = path.read_bytes()

    snapshot_b, _ = _append_child_snapshot(
        lineage=lineage,
        parent=snapshot_a,
        feature_set=feature_set,
    )
    materializer.materialize(
        dataset_snapshot=snapshot_b,
        feature_set=feature_set,
        feature_payload=_PAYLOAD,
    )
    assert path.read_bytes() != post_a

    path.write_bytes(post_a)
    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="monotonic history rejected workspace state",
    ):
        SourceFeatureArtifactAuthority.for_lineage(lineage)


def test_committed_publication_deletion_is_rejected_on_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autosport.source_feature_artifact_authority as module

    monkeypatch.setattr(module, "_utc_now", lambda: "2026-09-20T10:02:00Z")
    snapshot, feature_set, _, lineage = _context(tmp_path)
    materializer = SourceFeatureArtifactMaterializer(lineage)
    materializer.materialize(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_payload=_PAYLOAD,
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

    monkeypatch.setattr(module, "_utc_now", lambda: "2026-09-20T10:02:00Z")
    snapshot, feature_set, provenance, lineage = _context(tmp_path)
    materializer = SourceFeatureArtifactMaterializer(lineage)
    original_write = module.atomic_write_json

    def crash_before_publish(*_args, **_kwargs):
        raise RuntimeError("simulated crash before local publish")

    monkeypatch.setattr(module, "atomic_write_json", crash_before_publish)
    with pytest.raises(RuntimeError, match="simulated crash"):
        materializer.materialize(
            dataset_snapshot=snapshot,
            feature_set=feature_set,
            feature_payload=_PAYLOAD,
        )
    monkeypatch.setattr(module, "atomic_write_json", original_write)

    recovered = SourceFeatureArtifactMaterializer(lineage)
    record = recovered.materialize(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_payload=_PAYLOAD,
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
    import autosport.source_feature_artifact_authority as module

    monkeypatch.setattr(module, "_utc_now", lambda: "2026-09-20T10:02:00Z")
    snapshot, feature_set, provenance, lineage = _context(tmp_path)
    materializer = SourceFeatureArtifactMaterializer(lineage)

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
            feature_payload=_PAYLOAD,
        )

    recovered = SourceFeatureArtifactAuthority.for_lineage(lineage)
    record = recovered.resolve(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_provenance=provenance,
    )
    assert record.first_published_at_utc == "2026-09-20T10:02:00Z"


def test_direct_authority_mint_cannot_create_positive_availability(
    tmp_path: Path,
) -> None:
    snapshot, feature_set, provenance, lineage = _context(tmp_path)
    authority = SourceFeatureArtifactAuthority.for_lineage(lineage)

    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="direct feature publication is forbidden",
    ):
        authority.publish(
            dataset_snapshot=snapshot,
            feature_set=feature_set,
            feature_payload=_PAYLOAD,
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
