from __future__ import annotations

import hashlib
import json

import pytest

import autosport.point_in_time_evidence as point_in_time_module
from autosport import (
    _dataset_snapshot_lineage_publication_trust_root as lineage_trust_root,
)
from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    membership_manifest_sha256,
)
from autosport.point_in_time_evidence import (
    EvidenceLedgerCorruptError,
    FeatureArtifactProvenance,
    FutureEvidenceError,
    HoldoutAlreadyConsumedError,
    HoldoutConsumptionLedger,
    PointInTimeEvidenceError,
    PointInTimeFeatureAuthority,
)
from autosport.scientific_registry import DatasetSnapshot, FeatureSet, ScientificRegistry


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64
_HOLDOUT_MANIFEST_A = membership_manifest_sha256((_SHA_A,))
_HOLDOUT_MANIFEST_AB = membership_manifest_sha256((_SHA_A, _SHA_B))
_FEATURE_PAYLOAD = b"canonical-feature-payload-v1"


def _snapshot(
    *,
    snapshot_id: str = "snapshot-a",
    manifest_sha256: str = _HOLDOUT_MANIFEST_A,
    causal_cutoff: str = "2026-09-20T10:00:00Z",
    available_at: str = "2026-09-20T10:01:00Z",
    source_identity: str = "provider:canonical-feed",
    license_identity: str = "terms:v1",
) -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset_snapshot_id=snapshot_id,
        manifest_sha256=manifest_sha256,
        source_identity=source_identity,
        license_identity=license_identity,
        causal_cutoff=causal_cutoff,
        available_at_utc=available_at,
    )


def _feature_set(
    *,
    feature_set_id: str = "participant.form.mean.v1",
    version: str = "v17",
    definition_sha256: str = _SHA_C,
    source_sha256: str = _SHA_D,
    available_at: str = "2026-09-20T10:01:30Z",
) -> FeatureSet:
    return FeatureSet(
        feature_set_id=feature_set_id,
        version=version,
        definition_sha256=definition_sha256,
        source_sha256=source_sha256,
        available_at_utc=available_at,
    )


def _authority_root(tmp_path):
    return tmp_path.parent / f"{tmp_path.name}-machine-authority"


@pytest.fixture(autouse=True)
def _use_isolated_product_authority_root(tmp_path, monkeypatch):
    product_root = _authority_root(tmp_path).resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: product_root,
    )


def _members_for_manifest(manifest_sha256: str) -> tuple[str, ...]:
    if manifest_sha256 == _HOLDOUT_MANIFEST_A:
        return (_SHA_A,)
    if manifest_sha256 == _HOLDOUT_MANIFEST_AB:
        return (_SHA_A, _SHA_B)
    raise AssertionError("test holdout snapshot must use a canonical typed manifest")


def _holdout_lineage(tmp_path, *snapshots: DatasetSnapshot) -> DatasetSnapshotLineageAuthority:
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    for snapshot in snapshots:
        registry.append(snapshot)
    lineage = DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry,
        authority_root=_authority_root(tmp_path),
    )
    parent_snapshot_id: str | None = None
    for snapshot in snapshots:
        lineage.register(
            snapshot_id=snapshot.dataset_snapshot_id,
            member_sha256=_members_for_manifest(snapshot.manifest_sha256),
            parent_snapshot_id=parent_snapshot_id,
        )
        parent_snapshot_id = snapshot.dataset_snapshot_id
    return lineage


def _canonical_feature_context(
    tmp_path,
    *,
    snapshot_id: str = "snapshot-a",
    causal_cutoff: str = "2026-09-20T10:00:00Z",
    dataset_available_at: str = "2026-09-20T10:01:00Z",
    feature_available_at: str = "2026-09-20T10:01:30Z",
    feature_set_id: str = "participant.form.mean.v1",
    feature_payload: bytes = _FEATURE_PAYLOAD,
):
    feature_set = _feature_set(
        feature_set_id=feature_set_id,
        available_at=feature_available_at,
    )
    provisional_snapshot = _snapshot(
        snapshot_id=snapshot_id,
        causal_cutoff=causal_cutoff,
        available_at=dataset_available_at,
    )
    provenance = FeatureArtifactProvenance.issue(
        dataset_snapshot=provisional_snapshot,
        feature_set=feature_set,
        feature_payload=feature_payload,
    )
    members = (provenance.provenance_sha256,)
    snapshot = _snapshot(
        snapshot_id=snapshot_id,
        manifest_sha256=membership_manifest_sha256(members),
        causal_cutoff=causal_cutoff,
        available_at=dataset_available_at,
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
    return snapshot, feature_set, provenance, registry, lineage


def test_arbitrary_feature_bytes_plus_self_authored_lineage_cannot_mint_positive_evidence(tmp_path) -> None:
    snapshot, feature_set, provenance, _, lineage = _canonical_feature_context(
        tmp_path, feature_payload=b"caller-controlled-arbitrary-feature-bytes"
    )
    with pytest.raises(PointInTimeEvidenceError, match="independent source-owned feature artifact authority"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot, feature_set=feature_set,
            feature_provenance=provenance, lineage_authority=lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )


def test_equivalent_decision_cutoff_spellings_cannot_bypass_source_authority_gate(tmp_path) -> None:
    snapshot, feature_set, provenance, _, lineage = _canonical_feature_context(tmp_path)
    for cutoff in ("2099-01-01T00:00:00Z", "2099-01-01T02:00:00+02:00"):
        with pytest.raises(PointInTimeEvidenceError, match="independent source-owned feature artifact authority"):
            PointInTimeFeatureAuthority.bind(
                dataset_snapshot=snapshot, feature_set=feature_set,
                feature_provenance=provenance, lineage_authority=lineage,
                decision_cutoff_utc=cutoff,
            )


def test_unrelated_registered_feature_set_cannot_pair_with_dataset_snapshot(tmp_path) -> None:
    snapshot, _, _, registry, lineage = _canonical_feature_context(tmp_path)
    unrelated = _feature_set(
        feature_set_id="participant.form.unrelated.v1", version="v99",
        definition_sha256=_SHA_E, source_sha256=_SHA_B,
    )
    registry.append(unrelated)
    forged_relation = FeatureArtifactProvenance.issue(
        dataset_snapshot=snapshot, feature_set=unrelated, feature_payload=b"unrelated-payload"
    )
    with pytest.raises(PointInTimeEvidenceError, match="not committed"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot, feature_set=unrelated,
            feature_provenance=forged_relation, lineage_authority=lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )


def test_arbitrary_payload_digest_cannot_override_canonical_provenance(tmp_path) -> None:
    snapshot, feature_set, provenance, _, lineage = _canonical_feature_context(tmp_path)
    with pytest.raises(PointInTimeEvidenceError, match="audit digest"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot, feature_set=feature_set,
            feature_provenance=provenance, lineage_authority=lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z", feature_payload_sha256="f" * 64,
        )


def test_backfilled_provenance_cannot_become_known_before_authority_publication(tmp_path) -> None:
    snapshot, feature_set, provenance, _, lineage = _canonical_feature_context(tmp_path)
    with pytest.raises(FutureEvidenceError, match="not published"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot, feature_set=feature_set,
            feature_provenance=provenance, lineage_authority=lineage,
            decision_cutoff_utc="2026-09-20T19:00:00Z",
        )


def test_source_authority_gate_survives_lineage_restart(tmp_path) -> None:
    snapshot, feature_set, provenance, registry, lineage = _canonical_feature_context(tmp_path)
    with pytest.raises(PointInTimeEvidenceError, match="independent source-owned feature artifact authority"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot, feature_set=feature_set,
            feature_provenance=provenance, lineage_authority=lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )
    restarted = DatasetSnapshotLineageAuthority(
        lineage.path, registry,
        authority_root=lineage.monotonic_authority.authority_root,
        workspace_instance_id=lineage.monotonic_authority.workspace_instance_id,
    )
    restored_provenance = FeatureArtifactProvenance.from_payload(provenance.to_payload())
    with pytest.raises(PointInTimeEvidenceError, match="independent source-owned feature artifact authority"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot, feature_set=feature_set,
            feature_provenance=restored_provenance, lineage_authority=restarted,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )


def test_backfilled_feature_set_available_after_decision_fails_closed(tmp_path) -> None:
    snapshot, feature_set, provenance, _, lineage = _canonical_feature_context(
        tmp_path, feature_available_at="2026-09-20T10:03:00Z"
    )
    with pytest.raises(FutureEvidenceError, match="feature set was not available"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot, feature_set=feature_set,
            feature_provenance=provenance, lineage_authority=lineage,
            decision_cutoff_utc="2026-09-20T10:02:00Z",
        )


def test_dataset_snapshot_itself_must_be_available_by_decision(tmp_path) -> None:
    snapshot, feature_set, provenance, _, lineage = _canonical_feature_context(
        tmp_path, dataset_available_at="2026-09-20T10:03:00Z"
    )
    with pytest.raises(FutureEvidenceError, match="dataset snapshot"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot, feature_set=feature_set,
            feature_provenance=provenance, lineage_authority=lineage,
            decision_cutoff_utc="2026-09-20T10:02:00Z",
        )


def test_dataset_causal_cutoff_after_decision_fails_closed(tmp_path) -> None:
    snapshot, feature_set, provenance, _, lineage = _canonical_feature_context(
        tmp_path, causal_cutoff="2026-09-20T10:02:01Z"
    )
    with pytest.raises(FutureEvidenceError, match="causal cutoff"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot, feature_set=feature_set,
            feature_provenance=provenance, lineage_authority=lineage,
            decision_cutoff_utc="2026-09-20T10:02:00Z",
        )


def test_renamed_snapshot_cannot_mint_fresh_holdout_after_restart(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    original = _snapshot(snapshot_id="confirmation-window-name-a")
    alias = _snapshot(snapshot_id="renamed-confirmation-window-b")
    lineage = _holdout_lineage(tmp_path, original, alias)
    first = HoldoutConsumptionLedger(path, lineage_authority=lineage).consume(
        dataset_snapshot=original, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9", consumer_identity="experiment:challenger-a",
        purpose="final-confirmation", consumed_at_utc="2026-09-20T10:05:00Z",
    )
    restarted = HoldoutConsumptionLedger(path, lineage_authority=lineage)
    assert restarted.access_id(
        dataset_snapshot=alias, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
    ) == first.holdout_access_id
    assert restarted.freshness_id(
        dataset_snapshot=alias, confirmation_trial_family_id="family-9"
    ) == first.holdout_freshness_id
    with pytest.raises(HoldoutAlreadyConsumedError, match="already consumed"):
        restarted.consume(
            dataset_snapshot=alias, research_protocol_id="protocol-42",
            confirmation_trial_family_id="family-9", consumer_identity="experiment:challenger-b",
            purpose="fresh-confirmation", consumed_at_utc="2026-09-20T10:06:00Z",
        )


def test_caller_substituted_snapshot_identity_cannot_mint_fresh_holdout(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    canonical = _snapshot(snapshot_id="confirmation-window")
    lineage = _holdout_lineage(tmp_path, canonical)
    ledger = HoldoutConsumptionLedger(path, lineage_authority=lineage)
    ledger.consume(
        dataset_snapshot=canonical, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9", consumer_identity="experiment:challenger-a",
        purpose="final-confirmation", consumed_at_utc="2026-09-20T10:05:00Z",
    )
    for forged in (
        _snapshot(snapshot_id=canonical.dataset_snapshot_id, manifest_sha256=_HOLDOUT_MANIFEST_AB),
        _snapshot(snapshot_id=canonical.dataset_snapshot_id, source_identity="provider:forged-feed"),
        _snapshot(snapshot_id=canonical.dataset_snapshot_id, license_identity="terms:forged"),
    ):
        with pytest.raises(PointInTimeEvidenceError, match="does not match canonical dataset lineage authority"):
            ledger.consume(
                dataset_snapshot=forged, research_protocol_id="protocol-42",
                confirmation_trial_family_id="family-9", consumer_identity="experiment:challenger-b",
                purpose="forged-fresh-confirmation", consumed_at_utc="2026-09-20T10:06:00Z",
            )
    assert len(ledger.records()) == 1


def test_new_protocol_id_cannot_launder_consumed_physical_holdout(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    snapshot = _snapshot()
    lineage = _holdout_lineage(tmp_path, snapshot)
    ledger = HoldoutConsumptionLedger(path, lineage_authority=lineage)
    first = ledger.consume(
        dataset_snapshot=snapshot, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9", consumer_identity="experiment:challenger-a",
        purpose="final-confirmation", consumed_at_utc="2026-09-20T10:05:00Z",
    )
    assert ledger.access_id(
        dataset_snapshot=snapshot, research_protocol_id="protocol-43",
        confirmation_trial_family_id="family-9",
    ) != first.holdout_access_id
    with pytest.raises(HoldoutAlreadyConsumedError, match="already consumed"):
        ledger.consume(
            dataset_snapshot=snapshot, research_protocol_id="protocol-43",
            confirmation_trial_family_id="family-9", consumer_identity="experiment:challenger-b",
            purpose="new-protocol-confirmation", consumed_at_utc="2026-09-20T10:06:00Z",
        )


def test_family_alias_cannot_launder_consumed_physical_holdout_after_restart(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    original = _snapshot(snapshot_id="confirmation-window-original")
    alias = _snapshot(snapshot_id="confirmation-window-alias")
    lineage = _holdout_lineage(tmp_path, original, alias)
    first = HoldoutConsumptionLedger(path, lineage_authority=lineage).consume(
        dataset_snapshot=original,
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:challenger-a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )

    restarted = HoldoutConsumptionLedger(path, lineage_authority=lineage)
    assert restarted.freshness_id(
        dataset_snapshot=alias,
        confirmation_trial_family_id="family-renamed",
    ) != first.holdout_freshness_id

    with pytest.raises(HoldoutAlreadyConsumedError, match="already consumed"):
        restarted.assert_unused(
            dataset_snapshot=alias,
            research_protocol_id="protocol-43",
            confirmation_trial_family_id="family-renamed",
        )
    with pytest.raises(HoldoutAlreadyConsumedError, match="physical holdout"):
        restarted.consume(
            dataset_snapshot=alias,
            research_protocol_id="protocol-43",
            confirmation_trial_family_id="family-renamed",
            consumer_identity="experiment:challenger-b",
            purpose="renamed-family-confirmation",
            consumed_at_utc="2026-09-20T10:06:00Z",
        )
    assert restarted.records() == (first,)


def test_family_alias_guard_uses_captured_runtime_delegate(tmp_path, monkeypatch) -> None:
    path = tmp_path / "holdout_consumption.json"
    snapshot = _snapshot(snapshot_id="confirmation-window")
    lineage = _holdout_lineage(tmp_path, snapshot)
    ledger = HoldoutConsumptionLedger(path, lineage_authority=lineage)
    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:challenger-a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )

    monkeypatch.setattr(
        point_in_time_module,
        "_find_physical_holdout_consumption",
        lambda records, *, dataset_snapshot: None,
    )
    with pytest.raises(HoldoutAlreadyConsumedError, match="physical holdout"):
        ledger.consume(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-43",
            confirmation_trial_family_id="family-renamed",
            consumer_identity="experiment:challenger-b",
            purpose="renamed-family-confirmation",
            consumed_at_utc="2026-09-20T10:06:00Z",
        )


def test_exact_retry_is_idempotent_and_keeps_original_consumption_time(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    snapshot = _snapshot()
    lineage = _holdout_lineage(tmp_path, snapshot)
    ledger = HoldoutConsumptionLedger(path, lineage_authority=lineage)
    first = ledger.consume(
        dataset_snapshot=snapshot, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9", consumer_identity="experiment:challenger-a",
        purpose="final-confirmation", consumed_at_utc="2026-09-20T10:05:00Z",
    )
    retried = ledger.consume(
        dataset_snapshot=snapshot, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9", consumer_identity="experiment:challenger-a",
        purpose="final-confirmation", consumed_at_utc="2026-09-20T10:07:00Z",
    )
    assert retried == first
    assert retried.consumed_at_utc == "2026-09-20T10:05:00Z"
    assert len(ledger.records()) == 1


def test_holdout_cannot_be_consumed_before_snapshot_availability(tmp_path) -> None:
    snapshot = _snapshot(available_at="2026-09-20T10:10:00Z")
    lineage = _holdout_lineage(tmp_path, snapshot)
    with pytest.raises(FutureEvidenceError, match="before the dataset is available"):
        HoldoutConsumptionLedger(tmp_path / "holdout.json", lineage_authority=lineage).consume(
            dataset_snapshot=snapshot, research_protocol_id="protocol-42",
            confirmation_trial_family_id="family-9", consumer_identity="experiment:challenger-a",
            purpose="final-confirmation", consumed_at_utc="2026-09-20T10:09:59Z",
        )


def test_holdout_ledger_tamper_is_rejected_on_restart(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    snapshot = _snapshot()
    lineage = _holdout_lineage(tmp_path, snapshot)
    ledger = HoldoutConsumptionLedger(path, lineage_authority=lineage)
    ledger.consume(
        dataset_snapshot=snapshot, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9", consumer_identity="experiment:challenger-a",
        purpose="final-confirmation", consumed_at_utc="2026-09-20T10:05:00Z",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["records"][0]["consumer_identity"] = "experiment:forged"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EvidenceLedgerCorruptError, match="digest mismatch"):
        HoldoutConsumptionLedger(path, lineage_authority=lineage)


def test_redigested_identity_tamper_is_still_rejected(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    snapshot = _snapshot()
    lineage = _holdout_lineage(tmp_path, snapshot)
    HoldoutConsumptionLedger(path, lineage_authority=lineage).consume(
        dataset_snapshot=snapshot, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9", consumer_identity="experiment:challenger-a",
        purpose="final-confirmation", consumed_at_utc="2026-09-20T10:05:00Z",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["records"][0]["holdout_freshness_id"] = "f" * 64
    records_json = json.dumps(
        {"records": payload["records"]}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    payload["records_sha256"] = hashlib.sha256(records_json.encode("utf-8")).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EvidenceLedgerCorruptError, match="invalid holdout record"):
        HoldoutConsumptionLedger(path, lineage_authority=lineage)


def test_restoring_older_valid_ledger_is_rejected_by_external_monotonic_authority(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    authority_root = _authority_root(tmp_path)
    first_snapshot = _snapshot(snapshot_id="window-a")
    second_snapshot = _snapshot(snapshot_id="window-b", manifest_sha256=_HOLDOUT_MANIFEST_AB)
    lineage = _holdout_lineage(tmp_path, first_snapshot, second_snapshot)
    ledger = HoldoutConsumptionLedger(path, authority_root=authority_root, lineage_authority=lineage)
    ledger.consume(
        dataset_snapshot=first_snapshot, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9", consumer_identity="experiment:a",
        purpose="final-confirmation", consumed_at_utc="2026-09-20T10:05:00Z",
    )
    old_valid_bytes = path.read_bytes()
    ledger.consume(
        dataset_snapshot=second_snapshot, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-10", consumer_identity="experiment:b",
        purpose="final-confirmation", consumed_at_utc="2026-09-20T10:06:00Z",
    )
    path.write_bytes(old_valid_bytes)
    with pytest.raises(EvidenceLedgerCorruptError, match="monotonic authority"):
        HoldoutConsumptionLedger(path, authority_root=authority_root, lineage_authority=lineage)


def test_deleting_ledger_after_consumption_is_rejected_by_external_authority(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    authority_root = _authority_root(tmp_path)
    snapshot = _snapshot()
    lineage = _holdout_lineage(tmp_path, snapshot)
    HoldoutConsumptionLedger(path, authority_root=authority_root, lineage_authority=lineage).consume(
        dataset_snapshot=snapshot, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9", consumer_identity="experiment:a",
        purpose="final-confirmation", consumed_at_utc="2026-09-20T10:05:00Z",
    )
    path.unlink()
    with pytest.raises(EvidenceLedgerCorruptError, match="monotonic authority"):
        HoldoutConsumptionLedger(path, authority_root=authority_root, lineage_authority=lineage)


def test_crash_after_local_publish_recovers_pending_commit(tmp_path, monkeypatch) -> None:
    path = tmp_path / "holdout_consumption.json"
    authority_root = _authority_root(tmp_path)
    snapshot = _snapshot()
    lineage = _holdout_lineage(tmp_path, snapshot)
    ledger = HoldoutConsumptionLedger(path, authority_root=authority_root, lineage_authority=lineage)
    real_write = point_in_time_module._atomic_write_json

    class SimulatedCrash(RuntimeError):
        pass

    def write_then_crash(target, payload):
        real_write(target, payload)
        raise SimulatedCrash("after local publish")

    monkeypatch.setattr(point_in_time_module, "_atomic_write_json", write_then_crash)
    with pytest.raises(SimulatedCrash, match="after local publish"):
        ledger.consume(
            dataset_snapshot=snapshot, research_protocol_id="protocol-42",
            confirmation_trial_family_id="family-9", consumer_identity="experiment:a",
            purpose="final-confirmation", consumed_at_utc="2026-09-20T10:05:00Z",
        )
    monkeypatch.setattr(point_in_time_module, "_atomic_write_json", real_write)
    restarted = HoldoutConsumptionLedger(path, authority_root=authority_root, lineage_authority=lineage)
    assert len(restarted.records()) == 1
    assert restarted.records()[0].consumer_identity == "experiment:a"


def test_crash_before_local_publish_aborts_and_exact_retry_uses_fresh_transaction(tmp_path, monkeypatch) -> None:
    path = tmp_path / "holdout_consumption.json"
    authority_root = _authority_root(tmp_path)
    snapshot = _snapshot()
    lineage = _holdout_lineage(tmp_path, snapshot)
    ledger = HoldoutConsumptionLedger(path, authority_root=authority_root, lineage_authority=lineage)
    real_write = point_in_time_module._atomic_write_json

    class SimulatedCrash(RuntimeError):
        pass

    def crash_before_write(target, payload):
        raise SimulatedCrash("before local publish")

    monkeypatch.setattr(point_in_time_module, "_atomic_write_json", crash_before_write)
    with pytest.raises(SimulatedCrash, match="before local publish"):
        ledger.consume(
            dataset_snapshot=snapshot, research_protocol_id="protocol-42",
            confirmation_trial_family_id="family-9", consumer_identity="experiment:a",
            purpose="final-confirmation", consumed_at_utc="2026-09-20T10:05:00Z",
        )
    monkeypatch.setattr(point_in_time_module, "_atomic_write_json", real_write)
    restarted = HoldoutConsumptionLedger(path, authority_root=authority_root, lineage_authority=lineage)
    persisted = restarted.consume(
        dataset_snapshot=snapshot, research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9", consumer_identity="experiment:a",
        purpose="final-confirmation", consumed_at_utc="2026-09-20T10:05:00Z",
    )
    assert persisted.consumer_identity == "experiment:a"
    assert len(restarted.records()) == 1
