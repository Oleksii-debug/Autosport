from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from autosport.point_in_time_authority import (
    AvailabilityWitnessAuthority,
    FeatureAvailabilityError,
    FeatureAvailabilityEvidence,
    FeatureMembershipAuthority,
    HoldoutAlreadyConsumedError,
    HoldoutConsumptionError,
    HoldoutConsumptionLedger,
    RevisionPolicyAuthority,
    SourceRevisionAuthority,
    SourceRevisionAuthorityError,
    SourceRevisionAuthorityStore,
    holdout_identity,
)
from autosport.scientific_registry import (
    DatasetSnapshot,
    FeatureSet,
    ScientificRegistry,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
FEATURE_MANIFEST_PAYLOAD = {
    "schema": "autosport.feature_definition_manifest",
    "schema_version": 1,
    "features": {"participant.form.trailing_5": SHA_E},
}
FEATURE_MANIFEST_JSON = json.dumps(
    FEATURE_MANIFEST_PAYLOAD,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
)
FEATURE_MANIFEST_SHA256 = hashlib.sha256(
    FEATURE_MANIFEST_JSON.encode("utf-8")
).hexdigest()
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _witness_content(
    *,
    source_as_of: datetime,
    available_at: datetime,
) -> tuple[str, str]:
    payload = {
        "schema": "autosport.source_availability_witness",
        "schema_version": 1,
        "source_identity": "lawful-provider:fixture",
        "source_revision": "provider-revision-17",
        "source_revision_sha256": SHA_D,
        "witness_kind": "provider-publication-metadata",
        "source_as_of": _iso(source_as_of),
        "available_at": _iso(available_at),
    }
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    feature_set_id: str = "features-1",
    *,
    available_at: datetime = BASE + timedelta(minutes=15),
) -> FeatureSet:
    return FeatureSet(
        feature_set_id,
        "v1",
        FEATURE_MANIFEST_SHA256,
        SHA_C,
        _iso(available_at),
    )


def _registry(tmp_path) -> ScientificRegistry:
    return ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")


def _append_dataset_and_features(
    registry: ScientificRegistry,
    *,
    snapshot: DatasetSnapshot | None = None,
    feature_set: FeatureSet | None = None,
) -> tuple[DatasetSnapshot, FeatureSet]:
    dataset = snapshot or _snapshot()
    features = feature_set or _feature_set()
    registry.append(dataset)
    registry.append(features)
    return dataset, features


def _source_store(
    tmp_path,
    *,
    available_at: datetime = BASE + timedelta(minutes=30),
    source_as_of: datetime = BASE + timedelta(minutes=20),
    feature_available_at: datetime = BASE + timedelta(minutes=15),
) -> tuple[
    SourceRevisionAuthorityStore,
    RevisionPolicyAuthority,
    SourceRevisionAuthority,
]:
    store = SourceRevisionAuthorityStore.initialize_pristine(tmp_path)
    policy = RevisionPolicyAuthority(
        revision_policy_id="provider-publication-time-v1",
        source_identity="lawful-provider:fixture",
        policy_version="1",
        policy_content_sha256=SHA_E,
        witness_kind="provider-publication-metadata",
        frozen_at=BASE - timedelta(days=1),
    )
    store.register_policy(policy)
    witness_content_json, _ = _witness_content(
        source_as_of=source_as_of,
        available_at=available_at,
    )
    witness = AvailabilityWitnessAuthority.create(
        availability_witness_id="provider-publication:17",
        witness_content_json=witness_content_json,
        recorded_at=max(available_at, BASE + timedelta(minutes=35)),
    )
    store.register_witness(witness)
    membership = FeatureMembershipAuthority.create(
        feature_set_id="features-1",
        feature_set_version="v1",
        feature_definition_sha256=FEATURE_MANIFEST_SHA256,
        feature_manifest_json=FEATURE_MANIFEST_JSON,
        feature_source_sha256=SHA_C,
        feature_name="participant.form.trailing_5",
        available_at=feature_available_at,
    )
    store.register_feature_membership(membership)
    revision = SourceRevisionAuthority(
        source_revision_authority_id="source-authority-17",
        source_identity="lawful-provider:fixture",
        source_revision="provider-revision-17",
        source_revision_sha256=SHA_D,
        revision_policy_id=policy.revision_policy_id,
        revision_policy_record_sha256=policy.authority_sha256,
        availability_witness_id=witness.availability_witness_id,
        availability_witness_sha256=witness.witness_content_sha256,
        availability_witness_record_sha256=witness.authority_sha256,
        witness_kind=policy.witness_kind,
        source_as_of=source_as_of,
        available_at=available_at,
        recorded_at=max(available_at, BASE + timedelta(minutes=40)),
    )
    store.register_revision(revision)
    return store, policy, revision


def _evidence(
    tmp_path,
    *,
    decision_cutoff: datetime = BASE + timedelta(hours=1),
    source_available_at: datetime = BASE + timedelta(minutes=30),
    dataset_available_at: datetime = BASE + timedelta(minutes=10),
    feature_available_at: datetime = BASE + timedelta(minutes=15),
) -> FeatureAvailabilityEvidence:
    registry = _registry(tmp_path)
    snapshot = _snapshot(available_at=dataset_available_at)
    feature_set = _feature_set(available_at=feature_available_at)
    _append_dataset_and_features(
        registry,
        snapshot=snapshot,
        feature_set=feature_set,
    )
    store, _, revision = _source_store(
        tmp_path,
        available_at=source_available_at,
        feature_available_at=feature_available_at,
    )
    return FeatureAvailabilityEvidence.from_authorities(
        scientific_registry=registry,
        source_authority_store=store,
        dataset_snapshot_id=snapshot.dataset_snapshot_id,
        feature_set_id=feature_set.feature_set_id,
        feature_name="participant.form.trailing_5",
        source_revision_authority_id=revision.source_revision_authority_id,
        decision_cutoff=decision_cutoff,
    )


def _canonical_digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _recompute_ledger_digest(raw: dict[str, object]) -> None:
    body = {key: value for key, value in raw.items() if key != "ledger_sha256"}
    raw["ledger_sha256"] = _canonical_digest(body)


def test_feature_availability_resolves_registered_dataset_feature_policy_and_revision(
    tmp_path,
) -> None:
    evidence = _evidence(tmp_path)

    assert evidence.dataset_snapshot_id == "dataset-1"
    assert evidence.feature_set_id == "features-1"
    assert evidence.source_revision == "provider-revision-17"
    assert evidence.source_revision_sha256 == SHA_D
    _, expected_witness_sha256 = _witness_content(
        source_as_of=BASE + timedelta(minutes=20),
        available_at=BASE + timedelta(minutes=30),
    )
    assert evidence.availability_witness_sha256 == expected_witness_sha256
    assert evidence.witness_kind == "provider-publication-metadata"
    assert len(evidence.dataset_record_sha256) == 64
    assert len(evidence.feature_record_sha256) == 64
    assert len(evidence.source_revision_authority_sha256) == 64
    assert len(evidence.evidence_sha256) == 64
    assert evidence.to_payload()["schema_version"] == 3
    assert len(evidence.availability_witness_record_sha256) == 64
    assert len(evidence.feature_membership_id) == 64
    assert len(evidence.feature_membership_sha256) == 64
    assert evidence.feature_member_definition_sha256 == SHA_E


def test_feature_evidence_digest_is_stable_across_equivalent_timezone_offsets(
    tmp_path,
) -> None:
    registry = _registry(tmp_path)
    snapshot, feature_set = _append_dataset_and_features(registry)
    store, _, revision = _source_store(tmp_path)
    plus_two = timezone(timedelta(hours=2))

    utc_evidence = FeatureAvailabilityEvidence.from_authorities(
        scientific_registry=registry,
        source_authority_store=store,
        dataset_snapshot_id=snapshot.dataset_snapshot_id,
        feature_set_id=feature_set.feature_set_id,
        feature_name="participant.form.trailing_5",
        source_revision_authority_id=revision.source_revision_authority_id,
        decision_cutoff=BASE + timedelta(hours=1),
    )
    offset_evidence = FeatureAvailabilityEvidence.from_authorities(
        scientific_registry=registry,
        source_authority_store=store,
        dataset_snapshot_id=snapshot.dataset_snapshot_id,
        feature_set_id=feature_set.feature_set_id,
        feature_name="participant.form.trailing_5",
        source_revision_authority_id=revision.source_revision_authority_id,
        decision_cutoff=(BASE + timedelta(hours=1)).astimezone(plus_two),
    )

    assert offset_evidence.to_payload() == utc_evidence.to_payload()
    assert offset_evidence.evidence_sha256 == utc_evidence.evidence_sha256


def test_unregistered_canonical_looking_dataset_fails_closed(tmp_path) -> None:
    registry = _registry(tmp_path)
    feature_set = _feature_set()
    registry.append(feature_set)
    store, _, revision = _source_store(tmp_path)
    forged = _snapshot("caller-only-dataset")

    with pytest.raises(FeatureAvailabilityError, match="DatasetSnapshot record is missing"):
        FeatureAvailabilityEvidence.from_authorities(
            scientific_registry=registry,
            source_authority_store=store,
            dataset_snapshot_id=forged.dataset_snapshot_id,
            feature_set_id=feature_set.feature_set_id,
            feature_name="participant.form.trailing_5",
            source_revision_authority_id=revision.source_revision_authority_id,
            decision_cutoff=BASE + timedelta(hours=1),
        )


def test_unregistered_canonical_looking_feature_set_fails_closed(tmp_path) -> None:
    registry = _registry(tmp_path)
    snapshot = _snapshot()
    registry.append(snapshot)
    store, _, revision = _source_store(tmp_path)
    forged = _feature_set("caller-only-features")

    with pytest.raises(FeatureAvailabilityError, match="FeatureSet record is missing"):
        FeatureAvailabilityEvidence.from_authorities(
            scientific_registry=registry,
            source_authority_store=store,
            dataset_snapshot_id=snapshot.dataset_snapshot_id,
            feature_set_id=forged.feature_set_id,
            feature_name="participant.form.trailing_5",
            source_revision_authority_id=revision.source_revision_authority_id,
            decision_cutoff=BASE + timedelta(hours=1),
        )


def test_unknown_source_revision_authority_fails_closed(tmp_path) -> None:
    registry = _registry(tmp_path)
    snapshot, feature_set = _append_dataset_and_features(registry)
    store, _, _ = _source_store(tmp_path)

    with pytest.raises(FeatureAvailabilityError, match="cannot be resolved"):
        FeatureAvailabilityEvidence.from_authorities(
            scientific_registry=registry,
            source_authority_store=store,
            dataset_snapshot_id=snapshot.dataset_snapshot_id,
            feature_set_id=feature_set.feature_set_id,
            feature_name="participant.form.trailing_5",
            source_revision_authority_id="unknown-source-authority",
            decision_cutoff=BASE + timedelta(hours=1),
        )


def test_revision_with_arbitrary_policy_digest_cannot_be_registered(tmp_path) -> None:
    store, policy, _ = _source_store(tmp_path)
    forged = SourceRevisionAuthority(
        source_revision_authority_id="source-authority-forged",
        source_identity=policy.source_identity,
        source_revision="provider-revision-forged",
        source_revision_sha256=SHA_D,
        revision_policy_id=policy.revision_policy_id,
        revision_policy_record_sha256=SHA_A,
        availability_witness_id="provider-publication:forged",
        availability_witness_sha256=SHA_F,
        availability_witness_record_sha256=SHA_A,
        witness_kind=policy.witness_kind,
        source_as_of=BASE + timedelta(minutes=20),
        available_at=BASE + timedelta(minutes=30),
        recorded_at=BASE + timedelta(minutes=40),
    )

    with pytest.raises(SourceRevisionAuthorityError, match="policy digest mismatch"):
        store.register_revision(forged)


def test_availability_witness_content_is_hash_bound_and_canonical() -> None:
    content, digest = _witness_content(
        source_as_of=BASE + timedelta(minutes=20),
        available_at=BASE + timedelta(minutes=30),
    )
    witness = AvailabilityWitnessAuthority.create(
        availability_witness_id="provider-publication:17",
        witness_content_json=content,
        recorded_at=BASE + timedelta(minutes=40),
    )
    assert witness.witness_content_sha256 == digest

    forged = content.replace(
        _iso(BASE + timedelta(minutes=30)),
        _iso(BASE + timedelta(minutes=29)),
    )
    with pytest.raises(
        SourceRevisionAuthorityError,
        match="content does not match authority fields|content digest mismatch",
    ):
        AvailabilityWitnessAuthority(
            availability_witness_id=witness.availability_witness_id,
            source_identity=witness.source_identity,
            source_revision=witness.source_revision,
            source_revision_sha256=witness.source_revision_sha256,
            witness_kind=witness.witness_kind,
            witness_content_sha256=witness.witness_content_sha256,
            witness_content_json=forged,
            source_as_of=witness.source_as_of,
            available_at=witness.available_at,
            recorded_at=witness.recorded_at,
        )


def test_revision_rejects_unknown_or_forged_availability_witness(tmp_path) -> None:
    store, policy, revision = _source_store(tmp_path)

    unknown = SourceRevisionAuthority(
        source_revision_authority_id="source-authority-unknown-witness",
        source_identity=revision.source_identity,
        source_revision=revision.source_revision,
        source_revision_sha256=revision.source_revision_sha256,
        revision_policy_id=policy.revision_policy_id,
        revision_policy_record_sha256=policy.authority_sha256,
        availability_witness_id="provider-publication:unknown",
        availability_witness_sha256=revision.availability_witness_sha256,
        availability_witness_record_sha256=revision.availability_witness_record_sha256,
        witness_kind=revision.witness_kind,
        source_as_of=revision.source_as_of,
        available_at=revision.available_at,
        recorded_at=revision.recorded_at,
    )
    with pytest.raises(
        SourceRevisionAuthorityError,
        match="unknown availability witness",
    ):
        store.register_revision(unknown)

    forged_digest = SourceRevisionAuthority(
        source_revision_authority_id="source-authority-forged-witness",
        source_identity=revision.source_identity,
        source_revision=revision.source_revision,
        source_revision_sha256=revision.source_revision_sha256,
        revision_policy_id=policy.revision_policy_id,
        revision_policy_record_sha256=policy.authority_sha256,
        availability_witness_id=revision.availability_witness_id,
        availability_witness_sha256=SHA_A,
        availability_witness_record_sha256=revision.availability_witness_record_sha256,
        witness_kind=revision.witness_kind,
        source_as_of=revision.source_as_of,
        available_at=revision.available_at,
        recorded_at=revision.recorded_at,
    )
    with pytest.raises(
        SourceRevisionAuthorityError,
        match="witness content digest mismatch",
    ):
        store.register_revision(forged_digest)

    backdated = SourceRevisionAuthority(
        source_revision_authority_id="source-authority-backdated",
        source_identity=revision.source_identity,
        source_revision=revision.source_revision,
        source_revision_sha256=revision.source_revision_sha256,
        revision_policy_id=policy.revision_policy_id,
        revision_policy_record_sha256=policy.authority_sha256,
        availability_witness_id=revision.availability_witness_id,
        availability_witness_sha256=revision.availability_witness_sha256,
        availability_witness_record_sha256=revision.availability_witness_record_sha256,
        witness_kind=revision.witness_kind,
        source_as_of=revision.source_as_of,
        available_at=revision.available_at - timedelta(minutes=1),
        recorded_at=revision.recorded_at,
    )
    with pytest.raises(
        SourceRevisionAuthorityError,
        match="witness time mismatch",
    ):
        store.register_revision(backdated)


def test_feature_membership_cannot_mint_nonexistent_member(tmp_path) -> None:
    forged_manifest = json.dumps(
        {
            "schema": "autosport.feature_definition_manifest",
            "schema_version": 1,
            "features": {
                "participant.form.trailing_5": SHA_E,
                "participant.form.future_leak": SHA_A,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    with pytest.raises(
        SourceRevisionAuthorityError,
        match="manifest digest does not match",
    ):
        FeatureMembershipAuthority.create(
            feature_set_id="features-1",
            feature_set_version="v1",
            feature_definition_sha256=FEATURE_MANIFEST_SHA256,
            feature_manifest_json=forged_manifest,
            feature_source_sha256=SHA_C,
            feature_name="participant.form.future_leak",
            available_at=BASE + timedelta(minutes=15),
        )

    with pytest.raises(
        SourceRevisionAuthorityError,
        match="not present in canonical feature manifest",
    ):
        FeatureMembershipAuthority.create(
            feature_set_id="features-1",
            feature_set_version="v1",
            feature_definition_sha256=FEATURE_MANIFEST_SHA256,
            feature_manifest_json=FEATURE_MANIFEST_JSON,
            feature_source_sha256=SHA_C,
            feature_name="participant.form.future_leak",
            available_at=BASE + timedelta(minutes=15),
        )


def test_unknown_feature_name_cannot_reuse_registered_feature_set(tmp_path) -> None:
    registry = _registry(tmp_path)
    snapshot, feature_set = _append_dataset_and_features(registry)
    store, _, revision = _source_store(tmp_path)

    with pytest.raises(
        FeatureAvailabilityError,
        match="feature membership authority cannot be resolved",
    ):
        FeatureAvailabilityEvidence.from_authorities(
            scientific_registry=registry,
            source_authority_store=store,
            dataset_snapshot_id=snapshot.dataset_snapshot_id,
            feature_set_id=feature_set.feature_set_id,
            feature_name="participant.form.future_leak",
            source_revision_authority_id=revision.source_revision_authority_id,
            decision_cutoff=BASE + timedelta(hours=1),
        )


def test_backdated_caller_claim_cannot_override_registered_late_availability(
    tmp_path,
) -> None:
    registry = _registry(tmp_path)
    snapshot, feature_set = _append_dataset_and_features(registry)
    store, _, revision = _source_store(
        tmp_path,
        source_as_of=BASE - timedelta(days=1),
        available_at=BASE + timedelta(hours=2),
    )

    with pytest.raises(FeatureAvailabilityError, match="became available after"):
        FeatureAvailabilityEvidence.from_authorities(
            scientific_registry=registry,
            source_authority_store=store,
            dataset_snapshot_id=snapshot.dataset_snapshot_id,
            feature_set_id=feature_set.feature_set_id,
            feature_name="participant.form.trailing_5",
            source_revision_authority_id=revision.source_revision_authority_id,
            decision_cutoff=BASE + timedelta(hours=1),
        )


def test_dataset_and_feature_availability_are_resolved_from_registry(tmp_path) -> None:
    with pytest.raises(FeatureAvailabilityError, match="dataset snapshot"):
        _evidence(
            tmp_path,
            dataset_available_at=BASE + timedelta(hours=2),
            decision_cutoff=BASE + timedelta(hours=1),
        )

    other = tmp_path / "feature"
    other.mkdir()
    with pytest.raises(FeatureAvailabilityError, match="feature-set definition"):
        _evidence(
            other,
            feature_available_at=BASE + timedelta(hours=2),
            decision_cutoff=BASE + timedelta(hours=1),
        )


def test_source_authority_survives_restart_and_detects_tampering(tmp_path) -> None:
    store, policy, revision = _source_store(tmp_path)
    reopened = SourceRevisionAuthorityStore(tmp_path)
    assert reopened.resolve_policy(policy.revision_policy_id) == policy
    assert (
        reopened.resolve_revision(revision.source_revision_authority_id)
        == revision
    )

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["policies"][0]["policy_version"] = "tampered"
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SourceRevisionAuthorityError, match="record digest mismatch"):
        SourceRevisionAuthorityStore(tmp_path)


def test_holdout_identity_requires_exact_registered_dataset(tmp_path) -> None:
    registry = _registry(tmp_path)
    registered = _snapshot("dataset-registered")
    registry.append(registered)
    unregistered = _snapshot("dataset-unregistered")

    with pytest.raises(HoldoutConsumptionError, match="not the exact registered"):
        holdout_identity(
            scientific_registry=registry,
            dataset_snapshot=unregistered,
            research_protocol_id="protocol-1",
            confirmation_trial_family_id="family-1",
        )


def test_holdout_identity_is_stable_across_registered_dataset_aliases(tmp_path) -> None:
    registry = _registry(tmp_path)
    original = _snapshot("dataset-original")
    renamed = _snapshot("dataset-renamed")
    registry.append(original)
    registry.append(renamed)

    first = holdout_identity(
        scientific_registry=registry,
        dataset_snapshot=original,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
    )
    second = holdout_identity(
        scientific_registry=registry,
        dataset_snapshot=renamed,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
    )

    assert first == second
    assert len(first) == 64


def test_holdout_consumption_survives_restart_and_rejects_alias_reuse(tmp_path) -> None:
    registry = _registry(tmp_path)
    original = _snapshot("dataset-original")
    renamed = _snapshot("dataset-renamed")
    registry.append(original)
    registry.append(renamed)
    consumed_at = BASE + timedelta(hours=4)

    first = HoldoutConsumptionLedger(
        tmp_path,
        scientific_registry=registry,
    )
    receipt = first.consume(
        dataset_snapshot=original,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
        consumer_identity="evaluation-1",
        purpose="promotion-confirmation",
        consumed_at=consumed_at,
    )

    reopened = HoldoutConsumptionLedger(
        tmp_path,
        scientific_registry=ScientificRegistry(registry.path),
    )
    assert reopened.is_consumed(
        dataset_snapshot=renamed,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
    )
    assert reopened.receipt_for(receipt.holdout_access_id) == receipt

    with pytest.raises(HoldoutAlreadyConsumedError, match="already consumed"):
        reopened.consume(
            dataset_snapshot=renamed,
            research_protocol_id="protocol-1",
            confirmation_trial_family_id="family-1",
            consumer_identity="evaluation-2",
            purpose="second-look",
            consumed_at=consumed_at + timedelta(minutes=1),
        )


def test_holdout_requires_authoritative_outcome_reveal_evidence(tmp_path) -> None:
    registry = _registry(tmp_path)
    unknown_reveal = _snapshot(outcome_reveal_after=None)
    registry.append(unknown_reveal)
    ledger = HoldoutConsumptionLedger(tmp_path, scientific_registry=registry)

    with pytest.raises(HoldoutConsumptionError, match="reveal authority is missing"):
        ledger.consume(
            dataset_snapshot=unknown_reveal,
            research_protocol_id="protocol-1",
            confirmation_trial_family_id="family-1",
            consumer_identity="evaluation-1",
            purpose="promotion-confirmation",
            consumed_at=BASE + timedelta(hours=4),
        )

    assert ledger.receipts() == ()


def test_holdout_cannot_be_consumed_before_outcome_reveal(tmp_path) -> None:
    registry = _registry(tmp_path)
    snapshot = _snapshot(outcome_reveal_after=BASE + timedelta(hours=3))
    registry.append(snapshot)
    ledger = HoldoutConsumptionLedger(tmp_path, scientific_registry=registry)

    with pytest.raises(HoldoutConsumptionError, match="not revealed"):
        ledger.consume(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-1",
            confirmation_trial_family_id="family-1",
            consumer_identity="evaluation-1",
            purpose="too-early",
            consumed_at=BASE + timedelta(hours=2),
        )


def test_receipt_cannot_reset_semantic_identity_even_with_recomputed_digest(
    tmp_path,
) -> None:
    registry = _registry(tmp_path)
    snapshot = _snapshot()
    registry.append(snapshot)
    ledger = HoldoutConsumptionLedger(tmp_path, scientific_registry=registry)
    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
        consumer_identity="evaluation-1",
        purpose="promotion-confirmation",
        consumed_at=BASE + timedelta(hours=4),
    )

    raw = json.loads(ledger.path.read_text(encoding="utf-8"))
    receipt = raw["receipts"][0]
    receipt["holdout_access_id"] = SHA_F
    receipt_without_digest = {
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    }
    receipt["receipt_sha256"] = _canonical_digest(receipt_without_digest)
    _recompute_ledger_digest(raw)
    ledger.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(HoldoutConsumptionError, match="semantic consumption identity"):
        HoldoutConsumptionLedger(
            tmp_path,
            scientific_registry=registry,
        ).receipts()


def test_holdout_ledger_detects_receipt_tampering_on_restart(tmp_path) -> None:
    registry = _registry(tmp_path)
    snapshot = _snapshot()
    registry.append(snapshot)
    ledger = HoldoutConsumptionLedger(tmp_path, scientific_registry=registry)
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
        HoldoutConsumptionLedger(
            tmp_path,
            scientific_registry=registry,
        ).receipts()


def test_holdout_ledger_detects_valid_prefix_rollback_against_anchor(tmp_path) -> None:
    registry = _registry(tmp_path)
    snapshot = _snapshot()
    registry.append(snapshot)
    ledger = HoldoutConsumptionLedger(tmp_path, scientific_registry=registry)

    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
        consumer_identity="evaluation-1",
        purpose="first",
        consumed_at=BASE + timedelta(hours=4),
    )
    first_generation = ledger.path.read_text(encoding="utf-8")

    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-2",
        consumer_identity="evaluation-2",
        purpose="second",
        consumed_at=BASE + timedelta(hours=4, minutes=1),
    )
    ledger.path.write_text(first_generation, encoding="utf-8")

    with pytest.raises(HoldoutConsumptionError, match="rollback/tail mismatch"):
        HoldoutConsumptionLedger(
            tmp_path,
            scientific_registry=registry,
        ).receipts()


def test_holdout_ledger_detects_deleted_tail_even_if_root_digest_is_recomputed(
    tmp_path,
) -> None:
    registry = _registry(tmp_path)
    snapshot = _snapshot()
    registry.append(snapshot)
    ledger = HoldoutConsumptionLedger(tmp_path, scientific_registry=registry)

    for family in ("family-1", "family-2"):
        ledger.consume(
            dataset_snapshot=snapshot,
            research_protocol_id="protocol-1",
            confirmation_trial_family_id=family,
            consumer_identity=f"evaluation-{family}",
            purpose="promotion-confirmation",
            consumed_at=BASE + timedelta(hours=4),
        )

    raw = json.loads(ledger.path.read_text(encoding="utf-8"))
    raw["receipts"] = raw["receipts"][:1]
    raw["generation"] = 1
    raw["previous_ledger_sha256"] = "0" * 64
    _recompute_ledger_digest(raw)
    ledger.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(HoldoutConsumptionError, match="rollback/tail mismatch"):
        HoldoutConsumptionLedger(
            tmp_path,
            scientific_registry=registry,
        ).receipts()


def test_holdout_ledger_detects_missing_anchor_or_ledger_half(tmp_path) -> None:
    registry = _registry(tmp_path)
    snapshot = _snapshot()
    registry.append(snapshot)
    ledger = HoldoutConsumptionLedger(tmp_path, scientific_registry=registry)
    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
        consumer_identity="evaluation-1",
        purpose="promotion-confirmation",
        consumed_at=BASE + timedelta(hours=4),
    )
    ledger.anchor_path.unlink()

    with pytest.raises(HoldoutConsumptionError, match="completeness mismatch"):
        ledger.receipts()


def test_holdout_ledger_rejects_duplicate_json_keys(tmp_path) -> None:
    registry = _registry(tmp_path)
    snapshot = _snapshot()
    registry.append(snapshot)
    ledger = HoldoutConsumptionLedger(tmp_path, scientific_registry=registry)
    ledger.consume(
        dataset_snapshot=snapshot,
        research_protocol_id="protocol-1",
        confirmation_trial_family_id="family-1",
        consumer_identity="evaluation-1",
        purpose="promotion-confirmation",
        consumed_at=BASE + timedelta(hours=4),
    )
    good = json.loads(ledger.path.read_text(encoding="utf-8"))
    ledger.path.write_text(
        '{"schema":"autosport.holdout_consumption_ledger",'
        '"schema":"autosport.holdout_consumption_ledger",'
        f'"schema_version":2,"generation":{good["generation"]},'
        f'"previous_ledger_sha256":"{good["previous_ledger_sha256"]}",'
        '"receipts":[],"ledger_sha256":"' + ("0" * 64) + '"}',
        encoding="utf-8",
    )

    with pytest.raises(HoldoutConsumptionError, match="invalid holdout ledger JSON"):
        ledger.receipts()
