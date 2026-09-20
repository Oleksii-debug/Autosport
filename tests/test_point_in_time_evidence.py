from __future__ import annotations

import hashlib
import json

import pytest

import autosport.point_in_time_evidence as point_in_time_module
from autosport.point_in_time_evidence import (
    EvidenceLedgerCorruptError,
    FutureEvidenceError,
    HoldoutAlreadyConsumedError,
    HoldoutConsumptionLedger,
    PointInTimeFeatureAuthority,
)
from autosport.scientific_registry import DatasetSnapshot, FeatureSet


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64


def _snapshot(
    *,
    snapshot_id: str = "snapshot-a",
    manifest_sha256: str = _SHA_A,
    causal_cutoff: str = "2026-09-20T10:00:00Z",
    available_at: str = "2026-09-20T10:01:00Z",
) -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset_snapshot_id=snapshot_id,
        manifest_sha256=manifest_sha256,
        source_identity="provider:canonical-feed",
        license_identity="terms:v1",
        causal_cutoff=causal_cutoff,
        available_at_utc=available_at,
    )


def _feature_set(*, available_at: str = "2026-09-20T10:01:30Z") -> FeatureSet:
    return FeatureSet(
        feature_set_id="participant.form.mean.v1",
        version="v17",
        definition_sha256=_SHA_C,
        source_sha256=_SHA_D,
        available_at_utc=available_at,
    )


def _authority_root(tmp_path):
    return tmp_path.parent / f"{tmp_path.name}-machine-authority"


def test_point_in_time_feature_derives_authority_from_canonical_registry_records() -> None:
    evidence = PointInTimeFeatureAuthority.bind(
        dataset_snapshot=_snapshot(),
        feature_set=_feature_set(),
        feature_payload_sha256=_SHA_B,
        decision_cutoff_utc="2026-09-20T10:02:00Z",
    )

    assert evidence.feature_identity == "participant.form.mean.v1"
    assert evidence.feature_version == "v17"
    assert evidence.feature_definition_sha256 == _SHA_C
    assert evidence.source_revision == _SHA_D
    assert evidence.revision_policy_id == "scientific-registry-feature-set-v1"
    assert evidence.dataset_snapshot_id == "snapshot-a"
    assert evidence.dataset_manifest_sha256 == _SHA_A
    assert evidence.source_identity == "provider:canonical-feed"
    assert evidence.as_of_utc == "2026-09-20T10:00:00Z"
    assert evidence.available_at_utc == "2026-09-20T10:01:30Z"
    assert len(evidence.evidence_id) == 64


def test_equivalent_timezone_spellings_produce_same_feature_evidence_identity() -> None:
    zulu = PointInTimeFeatureAuthority.bind(
        dataset_snapshot=_snapshot(),
        feature_set=_feature_set(),
        feature_payload_sha256=_SHA_B,
        decision_cutoff_utc="2026-09-20T10:02:00Z",
    )
    offset = PointInTimeFeatureAuthority.bind(
        dataset_snapshot=_snapshot(
            causal_cutoff="2026-09-20T12:00:00+02:00",
            available_at="2026-09-20T12:01:00+02:00",
        ),
        feature_set=_feature_set(available_at="2026-09-20T12:01:30+02:00"),
        feature_payload_sha256=_SHA_B,
        decision_cutoff_utc="2026-09-20T12:02:00+02:00",
    )

    assert offset.evidence_id == zulu.evidence_id


def test_backfilled_feature_set_available_after_decision_fails_closed() -> None:
    with pytest.raises(FutureEvidenceError, match="feature set was not available"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=_snapshot(),
            feature_set=_feature_set(available_at="2026-09-20T10:03:00Z"),
            feature_payload_sha256=_SHA_B,
            decision_cutoff_utc="2026-09-20T10:02:00Z",
        )


def test_dataset_snapshot_itself_must_be_available_by_decision() -> None:
    with pytest.raises(FutureEvidenceError, match="dataset snapshot"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=_snapshot(available_at="2026-09-20T10:03:00Z"),
            feature_set=_feature_set(),
            feature_payload_sha256=_SHA_B,
            decision_cutoff_utc="2026-09-20T10:02:00Z",
        )


def test_dataset_causal_cutoff_after_decision_fails_closed() -> None:
    with pytest.raises(FutureEvidenceError, match="causal cutoff"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=_snapshot(causal_cutoff="2026-09-20T10:02:01Z"),
            feature_set=_feature_set(),
            feature_payload_sha256=_SHA_B,
            decision_cutoff_utc="2026-09-20T10:02:00Z",
        )


def test_renamed_snapshot_cannot_mint_fresh_holdout_after_restart(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    original = _snapshot(snapshot_id="confirmation-window-name-a")
    alias = _snapshot(snapshot_id="renamed-confirmation-window-b")

    first = HoldoutConsumptionLedger(path).consume(
        dataset_snapshot=original,
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:challenger-a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )

    restarted = HoldoutConsumptionLedger(path)
    assert restarted.access_id(
        dataset_snapshot=alias,
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
    ) == first.holdout_access_id
    assert restarted.freshness_id(
        dataset_snapshot=alias,
        confirmation_trial_family_id="family-9",
    ) == first.holdout_freshness_id

    with pytest.raises(HoldoutAlreadyConsumedError, match="already consumed"):
        restarted.consume(
            dataset_snapshot=alias,
            research_protocol_id="protocol-42",
            confirmation_trial_family_id="family-9",
            consumer_identity="experiment:challenger-b",
            purpose="fresh-confirmation",
            consumed_at_utc="2026-09-20T10:06:00Z",
        )


def test_new_protocol_id_cannot_launder_consumed_physical_holdout(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    ledger = HoldoutConsumptionLedger(path)
    snapshot = _snapshot()

    first = ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:challenger-a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )
    assert ledger.access_id(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-43",
        confirmation_trial_family_id="family-9",
    ) != first.holdout_access_id

    with pytest.raises(HoldoutAlreadyConsumedError, match="already consumed"):
        ledger.consume(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-43",
            confirmation_trial_family_id="family-9",
            consumer_identity="experiment:challenger-b",
            purpose="new-protocol-confirmation",
            consumed_at_utc="2026-09-20T10:06:00Z",
        )


def test_exact_retry_is_idempotent_and_keeps_original_consumption_time(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    ledger = HoldoutConsumptionLedger(path)
    snapshot = _snapshot()

    first = ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:challenger-a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )
    retried = ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:challenger-a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:07:00Z",
    )

    assert retried == first
    assert retried.consumed_at_utc == "2026-09-20T10:05:00Z"
    assert len(ledger.records()) == 1


def test_holdout_cannot_be_consumed_before_snapshot_availability(tmp_path) -> None:
    with pytest.raises(FutureEvidenceError, match="before the dataset is available"):
        HoldoutConsumptionLedger(tmp_path / "holdout.json").consume(
            dataset_snapshot=_snapshot(available_at="2026-09-20T10:10:00Z"),
            research_protocol_id="protocol-42",
            confirmation_trial_family_id="family-9",
            consumer_identity="experiment:challenger-a",
            purpose="final-confirmation",
            consumed_at_utc="2026-09-20T10:09:59Z",
        )


def test_holdout_ledger_tamper_is_rejected_on_restart(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    ledger = HoldoutConsumptionLedger(path)
    ledger.consume(
        dataset_snapshot=_snapshot(),
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:challenger-a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["records"][0]["consumer_identity"] = "experiment:forged"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceLedgerCorruptError, match="digest mismatch"):
        HoldoutConsumptionLedger(path)


def test_redigested_identity_tamper_is_still_rejected(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    HoldoutConsumptionLedger(path).consume(
        dataset_snapshot=_snapshot(),
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:challenger-a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["records"][0]["holdout_freshness_id"] = "f" * 64
    records_json = json.dumps(
        {"records": payload["records"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    payload["records_sha256"] = hashlib.sha256(records_json.encode("utf-8")).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceLedgerCorruptError, match="invalid holdout record"):
        HoldoutConsumptionLedger(path)


def test_restoring_older_valid_ledger_is_rejected_by_external_monotonic_authority(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    authority_root = _authority_root(tmp_path)
    ledger = HoldoutConsumptionLedger(path, authority_root=authority_root)

    ledger.consume(
        dataset_snapshot=_snapshot(snapshot_id="window-a", manifest_sha256=_SHA_A),
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )
    old_valid_bytes = path.read_bytes()

    ledger.consume(
        dataset_snapshot=_snapshot(snapshot_id="window-b", manifest_sha256=_SHA_B),
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-10",
        consumer_identity="experiment:b",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:06:00Z",
    )
    path.write_bytes(old_valid_bytes)

    with pytest.raises(EvidenceLedgerCorruptError, match="monotonic authority"):
        HoldoutConsumptionLedger(path, authority_root=authority_root)


def test_deleting_ledger_after_consumption_is_rejected_by_external_authority(tmp_path) -> None:
    path = tmp_path / "holdout_consumption.json"
    authority_root = _authority_root(tmp_path)
    HoldoutConsumptionLedger(path, authority_root=authority_root).consume(
        dataset_snapshot=_snapshot(),
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )
    path.unlink()

    with pytest.raises(EvidenceLedgerCorruptError, match="monotonic authority"):
        HoldoutConsumptionLedger(path, authority_root=authority_root)


def test_crash_after_local_publish_recovers_pending_commit(tmp_path, monkeypatch) -> None:
    path = tmp_path / "holdout_consumption.json"
    authority_root = _authority_root(tmp_path)
    ledger = HoldoutConsumptionLedger(path, authority_root=authority_root)
    real_write = point_in_time_module._atomic_write_json

    class SimulatedCrash(RuntimeError):
        pass

    def write_then_crash(target, payload):
        real_write(target, payload)
        raise SimulatedCrash("after local publish")

    monkeypatch.setattr(point_in_time_module, "_atomic_write_json", write_then_crash)
    with pytest.raises(SimulatedCrash, match="after local publish"):
        ledger.consume(
            dataset_snapshot=_snapshot(),
            research_protocol_id="protocol-42",
            confirmation_trial_family_id="family-9",
            consumer_identity="experiment:a",
            purpose="final-confirmation",
            consumed_at_utc="2026-09-20T10:05:00Z",
        )
    monkeypatch.setattr(point_in_time_module, "_atomic_write_json", real_write)

    restarted = HoldoutConsumptionLedger(path, authority_root=authority_root)
    assert len(restarted.records()) == 1
    assert restarted.records()[0].consumer_identity == "experiment:a"


def test_crash_before_local_publish_aborts_and_exact_retry_uses_fresh_transaction(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "holdout_consumption.json"
    authority_root = _authority_root(tmp_path)
    ledger = HoldoutConsumptionLedger(path, authority_root=authority_root)
    real_write = point_in_time_module._atomic_write_json

    class SimulatedCrash(RuntimeError):
        pass

    def crash_before_write(target, payload):
        raise SimulatedCrash("before local publish")

    monkeypatch.setattr(point_in_time_module, "_atomic_write_json", crash_before_write)
    with pytest.raises(SimulatedCrash, match="before local publish"):
        ledger.consume(
            dataset_snapshot=_snapshot(),
            research_protocol_id="protocol-42",
            confirmation_trial_family_id="family-9",
            consumer_identity="experiment:a",
            purpose="final-confirmation",
            consumed_at_utc="2026-09-20T10:05:00Z",
        )
    monkeypatch.setattr(point_in_time_module, "_atomic_write_json", real_write)

    restarted = HoldoutConsumptionLedger(path, authority_root=authority_root)
    persisted = restarted.consume(
        dataset_snapshot=_snapshot(),
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )
    assert persisted.consumer_identity == "experiment:a"
    assert len(restarted.records()) == 1
