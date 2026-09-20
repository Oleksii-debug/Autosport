from __future__ import annotations

import json

import pytest

from autosport.point_in_time_evidence import (
    EvidenceLedgerCorruptError,
    FutureEvidenceError,
    HoldoutAlreadyConsumedError,
    HoldoutConsumptionLedger,
    PointInTimeFeatureAuthority,
)
from autosport.scientific_registry import DatasetSnapshot


_SHA_A = "a" * 64
_SHA_B = "b" * 64


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


def test_point_in_time_feature_binds_existing_dataset_snapshot() -> None:
    evidence = PointInTimeFeatureAuthority.bind(
        dataset_snapshot=_snapshot(),
        feature_identity="participant.form.mean.v1",
        source_revision="provider-revision-17",
        revision_policy_id="as-known-at-decision-v1",
        feature_payload_sha256=_SHA_B,
        as_of_utc="2026-09-20T09:59:00Z",
        available_at_utc="2026-09-20T10:01:30Z",
        decision_cutoff_utc="2026-09-20T10:02:00Z",
    )

    assert evidence.dataset_snapshot_id == "snapshot-a"
    assert evidence.dataset_manifest_sha256 == _SHA_A
    assert evidence.source_identity == "provider:canonical-feed"
    assert len(evidence.evidence_id) == 64
    assert evidence.evidence_id == PointInTimeFeatureAuthority.bind(
        dataset_snapshot=_snapshot(),
        feature_identity="participant.form.mean.v1",
        source_revision="provider-revision-17",
        revision_policy_id="as-known-at-decision-v1",
        feature_payload_sha256=_SHA_B,
        as_of_utc="2026-09-20T09:59:00Z",
        available_at_utc="2026-09-20T10:01:30Z",
        decision_cutoff_utc="2026-09-20T10:02:00Z",
    ).evidence_id


def test_backfilled_feature_available_after_decision_fails_closed() -> None:
    with pytest.raises(FutureEvidenceError, match="not available"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=_snapshot(),
            feature_identity="participant.form.mean.v1",
            source_revision="backfill-revision-1",
            revision_policy_id="as-known-at-decision-v1",
            feature_payload_sha256=_SHA_B,
            as_of_utc="2026-09-19T12:00:00Z",
            available_at_utc="2026-09-20T10:03:00Z",
            decision_cutoff_utc="2026-09-20T10:02:00Z",
        )


def test_dataset_snapshot_itself_must_be_available_by_decision() -> None:
    with pytest.raises(FutureEvidenceError, match="dataset snapshot"):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=_snapshot(available_at="2026-09-20T10:03:00Z"),
            feature_identity="participant.form.mean.v1",
            source_revision="provider-revision-17",
            revision_policy_id="as-known-at-decision-v1",
            feature_payload_sha256=_SHA_B,
            as_of_utc="2026-09-20T09:59:00Z",
            available_at_utc="2026-09-20T10:01:30Z",
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

    with pytest.raises(HoldoutAlreadyConsumedError, match="already consumed"):
        restarted.consume(
            dataset_snapshot=alias,
            research_protocol_id="protocol-42",
            confirmation_trial_family_id="family-9",
            consumer_identity="experiment:challenger-b",
            purpose="fresh-confirmation",
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
