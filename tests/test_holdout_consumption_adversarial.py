from __future__ import annotations

from dataclasses import dataclass

import pytest

from autosport import _holdout_physical_content_guard as guard
from autosport import _strategy_model_factory_impl as factory
from autosport import point_in_time_evidence as evidence
from autosport.scientific_registry import promotion_holdout_access_id


SHA_A = "a" * 64
SHA_B = "b" * 64
T7 = "2026-01-08T00:00:00+00:00"


def _holdout_id(
    *,
    protocol_id: str = "protocol-factory",
    manifest_sha256: str = SHA_A,
    source_identity: str = "lawful-provider:fixture",
    license_identity: str = "license-evidence:v1",
    trial_family_id: str = "protocol-factory:confirmation-trial-family",
) -> str:
    return promotion_holdout_access_id(
        research_protocol_id=protocol_id,
        dataset_manifest_sha256=manifest_sha256,
        source_identity=source_identity,
        license_identity=license_identity,
        confirmation_trial_family_id=trial_family_id,
    )


def _attempt(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "experiment_id": "experiment-v2",
        "research_protocol_id": "protocol-factory",
        "research_question_id": "question-factory",
        "hypothesis_id": "hypothesis-factory",
        "candidate_strategy_version_id": "strategy-v2",
        "candidate_model_version_id": "model-v2",
        "evaluation_bundle_id": "eval-v2",
        "dataset_snapshot_id": "dataset-factory",
        "confirmation_trial_family_id": "protocol-factory:confirmation-trial-family",
        "holdout_access_id": _holdout_id(),
        "estimand": "mse",
        "direction": "LOWER_IS_BETTER",
        "rollback_identity": "strategy-v1",
        "created_at": T7,
    }
    payload.update(overrides)
    return payload


@dataclass(frozen=True)
class _Record:
    payload: dict[str, object]


class _Registry:
    def __init__(self, manifests: dict[str, str]) -> None:
        self._manifests = manifests

    def get(self, kind: str, record_id: str):
        assert kind == "DatasetSnapshot"
        manifest = self._manifests.get(record_id)
        if manifest is None:
            return None
        return _Record({"manifest_sha256": manifest})


def _consumption(
    *,
    manifest: str = SHA_A,
    protocol_id: str = "protocol-factory",
    source_identity: str = "lawful-provider:fixture",
    license_identity: str = "license-evidence:v1",
    trial_family_id: str = "protocol-factory:confirmation-trial-family",
    physical: bool,
) -> evidence.HoldoutConsumption:
    access = _holdout_id(
        protocol_id=protocol_id,
        manifest_sha256=manifest,
        source_identity=source_identity,
        license_identity=license_identity,
        trial_family_id=trial_family_id,
    )
    freshness_fn = (
        guard._physical_freshness_id if physical else guard._LEGACY_FRESHNESS_ID
    )
    freshness = freshness_fn(
        dataset_manifest_sha256=manifest,
        source_identity=source_identity,
        license_identity=license_identity,
        confirmation_trial_family_id=trial_family_id,
    )
    return evidence.HoldoutConsumption(
        holdout_access_id=access,
        holdout_freshness_id=freshness,
        research_protocol_id=protocol_id,
        confirmation_trial_family_id=trial_family_id,
        dataset_manifest_sha256=manifest,
        source_identity=source_identity,
        license_identity=license_identity,
        consumer_identity="factory:test",
        purpose="promotion confirmation",
        consumed_at_utc=T7,
    )


def test_exact_frozen_attempt_remains_resumable_for_same_physical_holdout() -> None:
    attempt = _attempt()
    registry = _Registry({"dataset-factory": SHA_A})

    assert not guard._holdout_consumed_by_physical_evidence(
        (dict(attempt),),
        same_attempt_identity=attempt,
        registry=registry,
    )


@pytest.mark.parametrize(
    ("field", "changed_value"),
    (
        ("experiment_id", "experiment-other"),
        ("research_protocol_id", "protocol-other"),
        ("research_question_id", "question-other"),
        ("hypothesis_id", "hypothesis-other"),
        ("candidate_strategy_version_id", "strategy-other"),
        ("candidate_model_version_id", "model-other"),
        ("evaluation_bundle_id", "eval-other"),
        ("dataset_snapshot_id", "dataset-alias"),
        ("confirmation_trial_family_id", "confirmation-other"),
        ("estimand", "log_loss"),
        ("direction", "HIGHER_IS_BETTER"),
        ("rollback_identity", "strategy-other"),
        ("created_at", "2026-01-09T00:00:00+00:00"),
    ),
)
def test_same_physical_holdout_is_consumed_when_frozen_attempt_changes(
    field: str,
    changed_value: object,
) -> None:
    current = _attempt()
    prior = _attempt(**{field: changed_value})
    registry = _Registry(
        {
            "dataset-factory": SHA_A,
            "dataset-alias": SHA_A,
        }
    )

    assert guard._holdout_consumed_by_physical_evidence(
        (prior,),
        same_attempt_identity=current,
        registry=registry,
    )


@pytest.mark.parametrize(
    "changed",
    (
        {
            "research_protocol_id": "protocol-other",
            "confirmation_trial_family_id": "protocol-other:confirmation-trial-family",
            "holdout_access_id": _holdout_id(
                protocol_id="protocol-other",
                trial_family_id="protocol-other:confirmation-trial-family",
            ),
        },
        {
            "holdout_access_id": _holdout_id(
                source_identity="lawful-provider:renamed",
            ),
        },
        {
            "holdout_access_id": _holdout_id(
                license_identity="license-evidence:v2",
            ),
        },
        {
            "confirmation_trial_family_id": "confirmation-family-renamed",
            "holdout_access_id": _holdout_id(
                trial_family_id="confirmation-family-renamed",
            ),
        },
    ),
)
def test_metadata_relabel_cannot_mint_fresh_capacity_for_same_manifest(
    changed: dict[str, object],
) -> None:
    current = _attempt()
    prior = _attempt(
        experiment_id="experiment-prior",
        dataset_snapshot_id="dataset-alias",
        **changed,
    )
    registry = _Registry(
        {
            "dataset-factory": SHA_A,
            "dataset-alias": SHA_A,
        }
    )

    assert prior["holdout_access_id"] != current["holdout_access_id"]
    assert guard._holdout_consumed_by_physical_evidence(
        (prior,),
        same_attempt_identity=current,
        registry=registry,
    )


def test_distinct_content_manifest_without_membership_fails_closed() -> None:
    current = _attempt()
    prior = _attempt(
        experiment_id="experiment-prior",
        dataset_snapshot_id="dataset-other",
        holdout_access_id=_holdout_id(manifest_sha256=SHA_B),
    )
    registry = _Registry(
        {
            "dataset-factory": SHA_A,
            "dataset-other": SHA_B,
        }
    )

    assert guard._holdout_consumed_by_physical_evidence(
        (prior,),
        same_attempt_identity=current,
        registry=registry,
    )


def test_missing_prior_dataset_authority_fails_closed() -> None:
    current = _attempt()
    prior = _attempt(
        experiment_id="experiment-prior",
        dataset_snapshot_id="missing-dataset",
        holdout_access_id=_holdout_id(source_identity="other-label"),
    )
    registry = _Registry({"dataset-factory": SHA_A})

    with pytest.raises(ValueError, match="missing DatasetSnapshot"):
        guard._holdout_consumed_by_physical_evidence(
            (prior,),
            same_attempt_identity=current,
            registry=registry,
        )


def test_product_factory_helper_uses_registry_physical_identity_context() -> None:
    current = _attempt()
    prior = _attempt(
        experiment_id="experiment-prior",
        dataset_snapshot_id="dataset-alias",
        holdout_access_id=_holdout_id(source_identity="renamed-source"),
    )
    registry = _Registry(
        {
            "dataset-factory": SHA_A,
            "dataset-alias": SHA_A,
        }
    )
    token = guard._ACTIVE_FACTORY_REGISTRY.set(registry)
    try:
        assert factory._holdout_consumed_by_other_evidence(
            (prior,),
            same_attempt_identity=current,
        )
    finally:
        guard._ACTIVE_FACTORY_REGISTRY.reset(token)


def test_physical_freshness_id_ignores_provenance_and_family_labels() -> None:
    baseline = guard._physical_freshness_id(
        dataset_manifest_sha256=SHA_A,
        source_identity="source-a",
        license_identity="license-a",
        confirmation_trial_family_id="family-a",
    )
    relabelled = guard._physical_freshness_id(
        dataset_manifest_sha256=SHA_A,
        source_identity="source-b",
        license_identity="license-b",
        confirmation_trial_family_id="family-b",
    )
    distinct = guard._physical_freshness_id(
        dataset_manifest_sha256=SHA_B,
        source_identity="source-a",
        license_identity="license-a",
        confirmation_trial_family_id="family-a",
    )

    assert baseline == relabelled
    assert distinct != baseline


def test_legacy_and_physical_ledger_records_are_both_readable() -> None:
    legacy = _consumption(physical=False)
    physical = _consumption(
        manifest=SHA_B,
        protocol_id="protocol-b",
        source_identity="source-b",
        license_identity="license-b",
        trial_family_id="family-b",
        physical=True,
    )

    assert legacy.holdout_freshness_id != guard._physical_freshness_id(
        dataset_manifest_sha256=SHA_A,
        source_identity=legacy.source_identity,
        license_identity=legacy.license_identity,
        confirmation_trial_family_id=legacy.confirmation_trial_family_id,
    )
    assert physical.holdout_freshness_id == guard._physical_freshness_id(
        dataset_manifest_sha256=SHA_B,
        source_identity=physical.source_identity,
        license_identity=physical.license_identity,
        confirmation_trial_family_id=physical.confirmation_trial_family_id,
    )


def test_legacy_load_reindexes_by_physical_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = _consumption(physical=False)

    class _Ledger:
        _records = {legacy.holdout_freshness_id: legacy}

    monkeypatch.setattr(guard, "_ORIGINAL_LEDGER_LOAD", lambda self: None)
    ledger = _Ledger()
    guard._load_with_physical_reindex(ledger)

    expected = guard._physical_freshness_id(
        dataset_manifest_sha256=legacy.dataset_manifest_sha256,
        source_identity=legacy.source_identity,
        license_identity=legacy.license_identity,
        confirmation_trial_family_id=legacy.confirmation_trial_family_id,
    )
    assert ledger._records == {expected: legacy}


def test_conflicting_legacy_aliases_for_same_manifest_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _consumption(physical=False)
    second = _consumption(
        protocol_id="protocol-other",
        source_identity="renamed-source",
        license_identity="renamed-license",
        trial_family_id="renamed-family",
        physical=False,
    )
    assert first.holdout_freshness_id != second.holdout_freshness_id

    class _Ledger:
        _records = {
            first.holdout_freshness_id: first,
            second.holdout_freshness_id: second,
        }

    monkeypatch.setattr(guard, "_ORIGINAL_LEDGER_LOAD", lambda self: None)
    with pytest.raises(
        evidence.EvidenceLedgerCorruptError,
        match="multiple consumptions for one physical content manifest",
    ):
        guard._load_with_physical_reindex(_Ledger())
