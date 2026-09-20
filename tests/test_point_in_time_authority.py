from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from autosport.point_in_time_authority import (
    FeatureAvailabilityError,
    FeatureAvailabilityEvidence,
    HoldoutAlreadyConsumedError,
    HoldoutConsumptionError,
    HoldoutConsumptionLedger,
    PointInTimeAuthorityError,
    holdout_identity,
)
from autosport.scientific_registry import DatasetSnapshot, FeatureSet


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _snapshot(
    snapshot_id: str = "dataset-1",
    *,
    available_at: datetime = BASE + timedelta(minutes=10),
    causal_cutoff: datetime = BASE,
    outcome_reveal_after: datetime | None = BASE + timedelta(hours=3),
) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id,
        SHA_A,
        "lawful-provider:fixture",
        "license-evidence:v1",
        _iso(causal_cutoff),
        _iso(available_at),
        outcome_reveal_after=(
            None if outcome_reveal_after is None else _iso(outcome_reveal_after)
        ),
    )


def _feature_set(
    *,
    available_at: datetime = BASE + timedelta(minutes=15),
) -> FeatureSet:
    return FeatureSet(
        "features-1",
        "v1",
        SHA_B,
        SHA_C,
        _iso(available_at),
    )


def _evidence(
    *,
    dataset_snapshot: DatasetSnapshot | None = None,
    feature_set: FeatureSet | None = None,
    source_as_of: datetime = BASE + timedelta(minutes=20),
    available_at: datetime = BASE + timedelta(minutes=30),
    decision_cutoff: datetime = BASE + timedelta(hours=1),
) -> FeatureAvailabilityEvidence:
    return FeatureAvailabilityEvidence.from_canonical(
        dataset_snapshot=dataset_snapshot or _snapshot(),
        feature_set=feature_set or _feature_set(),
        feature_name="participant.form.trailing_5",
        source_revision="provider-revision-17",
        source_revision_sha256=SHA_D,
        revision_policy_id="provider-publication-time-v1",
        revision_policy_sha256=SHA_E,
        source_as_of=source_as_of,
        available_at=available_at,
        decision_cutoff=decision_cutoff,
    )


def test_feature_availability_evidence_binds_canonical_dataset_and_feature_records() -> None:
    evidence = _evidence()

    assert evidence.dataset_snapshot_id == "dataset-1"
    assert evidence.dataset_manifest_sha256 == SHA_A
    assert evidence.feature_set_id == "features-1"
    assert evidence.feature_definition_sha256 == SHA_B
    assert evidence.feature_source_sha256 == SHA_C
    assert evidence.source_revision_sha256 == SHA_D
    assert evidence.revision_policy_sha256 == SHA_E
    assert len(evidence.evidence_sha256) == 64
    assert evidence.to_payload()["available_at"] == "2026-01-01T00:30:00Z"


def test_feature_evidence_digest_is_stable_across_equivalent_timezone_offsets() -> None:
    utc_evidence = _evidence()
    plus_two = timezone(timedelta(hours=2))
    offset_evidence = _evidence(
        source_as_of=(BASE + timedelta(minutes=20)).astimezone(plus_two),
        available_at=(BASE + timedelta(minutes=30)).astimezone(plus_two),
        decision_cutoff=(BASE + timedelta(hours=1)).astimezone(plus_two),
    )

    assert offset_evidence.to_payload() == utc_evidence.to_payload()
    assert offset_evidence.evidence_sha256 == utc_evidence.evidence_sha256


def test_future_feature_revision_fails_closed() -> None:
    with pytest.raises(FeatureAvailabilityError, match="became available after"):
        _evidence(
            available_at=BASE + timedelta(hours=2),
            decision_cutoff=BASE + timedelta(hours=1),
        )


def test_backfilled_source_does_not_borrow_its_historical_as_of_time() -> None:
    with pytest.raises(FeatureAvailabilityError, match="became available after"):
        _evidence(
            source_as_of=BASE - timedelta(days=1),
            available_at=BASE + timedelta(hours=2),
            decision_cutoff=BASE + timedelta(hours=1),
        )


def test_dataset_snapshot_must_itself_be_available_at_decision_time() -> None:
    snapshot = _snapshot(available_at=BASE + timedelta(hours=2))
    with pytest.raises(FeatureAvailabilityError, match="dataset snapshot"):
        _evidence(
            dataset_snapshot=snapshot,
            decision_cutoff=BASE + timedelta(hours=1),
        )


def test_feature_definition_must_be_available_at_decision_time() -> None:
    feature_set = _feature_set(available_at=BASE + timedelta(hours=2))
    with pytest.raises(FeatureAvailabilityError, match="feature-set definition"):
        _evidence(
            feature_set=feature_set,
            decision_cutoff=BASE + timedelta(hours=1),
        )


def test_revision_policy_identity_is_mandatory_not_caller_omissible() -> None:
    with pytest.raises(PointInTimeAuthorityError, match="revision_policy_id"):
        FeatureAvailabilityEvidence.from_canonical(
            dataset_snapshot=_snapshot(),
            feature_set=_feature_set(),
            feature_name="participant.form.trailing_5",
            source_revision="provider-revision-17",
            source_revision_sha256=SHA_D,
            revision_policy_id="",
            revision_policy_sha256=SHA_E,
            source_as_of=BASE + timedelta(minutes=20),
            available_at=BASE + timedelta(minutes=30),
            decision_cutoff=BASE + timedelta(hours=1),
        )


def test_holdout_identity_is_stable_across_dataset_snapshot_aliases() -> None:
    original = _snapshot("dataset-original")
    renamed = _snapshot("dataset-renamed")

    first = holdout_identity(
        dataset_snapshot=original,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
    )
    second = holdout_identity(
        dataset_snapshot=renamed,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
    )

    assert first == second
    assert len(first) == 64


def test_holdout_consumption_survives_restart_and_rejects_alias_reuse(tmp_path) -> None:
    original = _snapshot("dataset-original")
    renamed = _snapshot("dataset-renamed")
    consumed_at = BASE + timedelta(hours=4)

    first = HoldoutConsumptionLedger(tmp_path)
    receipt = first.consume(
        dataset_snapshot=original,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
        consumer_identity="evaluation-1",
        purpose="promotion-confirmation",
        consumed_at=consumed_at,
    )
    assert receipt.dataset_snapshot_id == "dataset-original"

    reopened = HoldoutConsumptionLedger(tmp_path)
    assert reopened.is_consumed(
        dataset_snapshot=renamed,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
    )
    loaded = reopened.receipt_for(receipt.holdout_access_id)
    assert loaded == receipt

    with pytest.raises(HoldoutAlreadyConsumedError, match="already consumed"):
        reopened.consume(
            dataset_snapshot=renamed,
            research_protocol_id="protocol-1",
            confirmation_trial_family_id="family-1",
            consumer_identity="evaluation-2",
            purpose="second-look",
            consumed_at=consumed_at + timedelta(minutes=1),
        )


def test_distinct_confirmation_family_has_distinct_consumption_identity(tmp_path) -> None:
    snapshot = _snapshot()
    ledger = HoldoutConsumptionLedger(tmp_path)
    consumed_at = BASE + timedelta(hours=4)

    first = ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
        consumer_identity="evaluation-1",
        purpose="confirmation-a",
        consumed_at=consumed_at,
    )
    second = ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-2",
        consumer_identity="evaluation-2",
        purpose="confirmation-b",
        consumed_at=consumed_at + timedelta(minutes=1),
    )

    assert first.holdout_access_id != second.holdout_access_id
    assert len(ledger.receipts()) == 2


def test_holdout_cannot_be_consumed_before_outcome_reveal(tmp_path) -> None:
    snapshot = _snapshot(outcome_reveal_after=BASE + timedelta(hours=3))
    ledger = HoldoutConsumptionLedger(tmp_path)

    with pytest.raises(HoldoutConsumptionError, match="not revealed"):
        ledger.consume(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-1",
            confirmation_trial_family_id="family-1",
            consumer_identity="evaluation-1",
            purpose="too-early",
            consumed_at=BASE + timedelta(hours=2),
        )

    assert ledger.receipts() == ()


def test_holdout_ledger_detects_receipt_tampering_on_restart(tmp_path) -> None:
    snapshot = _snapshot()
    ledger = HoldoutConsumptionLedger(tmp_path)
    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
        consumer_identity="evaluation-1",
        purpose="promotion-confirmation",
        consumed_at=BASE + timedelta(hours=4),
    )

    raw = json.loads(ledger.path.read_text(encoding="utf-8"))
    raw["receipts"][0]["consumer_identity"] = "tampered-evaluation"
    ledger.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(HoldoutConsumptionError, match="digest mismatch"):
        HoldoutConsumptionLedger(tmp_path).receipts()


def test_holdout_ledger_rejects_duplicate_json_keys(tmp_path) -> None:
    ledger = HoldoutConsumptionLedger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        '{"schema":"autosport.holdout_consumption_ledger",'
        '"schema":"autosport.holdout_consumption_ledger",'
        '"schema_version":1,"receipts":[]}',
        encoding="utf-8",
    )

    with pytest.raises(HoldoutConsumptionError, match="invalid holdout ledger JSON"):
        ledger.receipts()
