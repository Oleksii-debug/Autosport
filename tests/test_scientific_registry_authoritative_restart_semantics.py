from __future__ import annotations

from dataclasses import replace
import json

import pytest

from autosport.scientific_registry import (
    DuplicateExperimentFingerprintError,
    EvaluationBundleRef,
    PromotionAction,
    PromotionDecision,
    PromotionEvidenceError,
    ResearchOutcome,
    ScientificRegistry,
)
from test_scientific_registry import (
    SHA_A,
    SHA_B,
    SHA_C,
    T2,
    T3,
    _experiment,
    _foundation,
    _promotion_evidence,
)


def _write_state(path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_authoritative_reader_rejects_unproven_negative_repeat_before_tofu(
    tmp_path,
    monkeypatch,
):
    authority_root = tmp_path / "machine-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
    )

    source = ScientificRegistry.initialize_pristine(
        tmp_path / "source-workspace" / "scientific_registry.json"
    )
    _foundation(source)
    original = _experiment()
    source.append(original)
    valid_bytes = source.path.read_bytes()

    state = json.loads(valid_bytes.decode("utf-8"))
    repeat = replace(
        original,
        experiment_id="experiment-semantic-forgery",
        created_at=T3,
        completed_at=T3,
    )
    state["records"].append(ScientificRegistry._entry(repeat))

    legacy_path = tmp_path / "legacy-workspace" / "scientific_registry.json"
    _write_state(legacy_path, state)

    with pytest.raises(
        DuplicateExperimentFingerprintError,
        match="persisted negative-result repeat lacks durable repeat provenance",
    ):
        ScientificRegistry(legacy_path)

    # Failed semantic validation must happen before TOFU establishes independent
    # machine authority for the invalid image. Replacing it with the valid prefix
    # must therefore remain eligible for the first authoritative open.
    legacy_path.write_bytes(valid_bytes)
    reopened = ScientificRegistry(legacy_path)
    assert reopened.get("Experiment", "experiment-1") is not None
    assert reopened.get("Experiment", "experiment-semantic-forgery") is None


def test_authoritative_reader_rejects_boolean_schema_before_tofu(
    tmp_path,
    monkeypatch,
):
    authority_root = tmp_path / "machine-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
    )

    source = ScientificRegistry.initialize_pristine(
        tmp_path / "source-schema-workspace" / "scientific_registry.json"
    )
    _foundation(source)
    source.append(_experiment())
    valid_bytes = source.path.read_bytes()

    state = json.loads(valid_bytes.decode("utf-8"))
    state["schema_version"] = True

    legacy_path = tmp_path / "legacy-schema-workspace" / "scientific_registry.json"
    _write_state(legacy_path, state)

    with pytest.raises(ValueError, match="scientific registry schema_version mismatch"):
        ScientificRegistry(legacy_path)

    # A rejected noncanonical schema image must not become the monotonic baseline.
    legacy_path.write_bytes(valid_bytes)
    reopened = ScientificRegistry(legacy_path)
    assert reopened.get("Experiment", "experiment-1") is not None


def _promotion_shaped_bundle() -> EvaluationBundleRef:
    return EvaluationBundleRef(
        "eval-orphan-promotion",
        SHA_A,
        SHA_B,
        "dataset-1",
        SHA_C,
        (SHA_A, SHA_B),
        T2,
        evaluated_strategy_version_id="strategy-1",
        evaluated_model_version_id="model-1",
        effective_sample_size=1_000_000,
        effect_interval_low="0.1",
        effect_interval_high="0.3",
        practical_improvement="0.2",
    )


def test_authoritative_reader_rejects_orphan_promotion_ess_before_tofu(
    tmp_path,
    monkeypatch,
):
    authority_root = tmp_path / "machine-authority-ess-orphan"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
    )

    source = ScientificRegistry.initialize_pristine(
        tmp_path / "source-ess-orphan" / "scientific_registry.json"
    )
    valid_bytes = source.path.read_bytes()
    state = json.loads(valid_bytes.decode("utf-8"))
    state["records"].append(ScientificRegistry._entry(_promotion_shaped_bundle()))

    legacy_path = tmp_path / "legacy-ess-orphan" / "scientific_registry.json"
    _write_state(legacy_path, state)

    with pytest.raises(ValueError, match="canonical scientific lineage"):
        ScientificRegistry(legacy_path)

    # Rejection must happen before TOFU baseline publication.
    legacy_path.write_bytes(valid_bytes)
    reopened = ScientificRegistry(legacy_path)
    assert reopened.get("EvaluationBundle", "eval-orphan-promotion") is None


def test_authoritative_reader_rejects_backfilled_promotion_lineage_before_tofu(
    tmp_path,
    monkeypatch,
):
    authority_root = tmp_path / "machine-authority-ess-order"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
    )

    source = ScientificRegistry.initialize_pristine(
        tmp_path / "source-ess-order" / "scientific_registry.json"
    )
    foundation = _foundation(source)
    valid_bytes = source.path.read_bytes()
    state = json.loads(valid_bytes.decode("utf-8"))

    bundle_id = foundation["bundle"].evaluation_bundle_id
    bundle_index = next(
        index
        for index, raw in enumerate(state["records"])
        if raw["record_type"] == "EvaluationBundle"
        and raw["record_id"] == bundle_id
    )
    bundle_entry = state["records"].pop(bundle_index)
    state["records"].insert(0, bundle_entry)

    legacy_path = tmp_path / "legacy-ess-order" / "scientific_registry.json"
    _write_state(legacy_path, state)

    with pytest.raises(ValueError, match="durably recorded first"):
        ScientificRegistry(legacy_path)

    # Later/backfilled rows cannot become retroactive causal authority and a
    # rejected image must not poison the first valid machine baseline.
    legacy_path.write_bytes(valid_bytes)
    reopened = ScientificRegistry(legacy_path)
    assert reopened.get("EvaluationBundle", bundle_id) is not None


def test_authoritative_reader_rejects_legacy_positive_promotion_before_tofu(
    tmp_path,
    monkeypatch,
):
    authority_root = tmp_path / "machine-authority-legacy-promotion"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
    )

    source = ScientificRegistry.initialize_pristine(
        tmp_path / "source-legacy-promotion" / "scientific_registry.json"
    )
    foundation = _foundation(source)
    source.append(_experiment(outcome=ResearchOutcome.POSITIVE))
    evidence = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="legacy-positive-promotion",
        rollback_identity="NONE",
    )
    source.append(evidence)
    valid_bytes = source.path.read_bytes()
    state = json.loads(valid_bytes.decode("utf-8"))

    decision = PromotionDecision(
        "legacy-positive-promotion-decision",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence.promotion_evidence_id,
    )
    state["records"].append(ScientificRegistry._entry(decision))

    legacy_path = (
        tmp_path / "legacy-positive-promotion" / "scientific_registry.json"
    )
    _write_state(legacy_path, state)

    with pytest.raises(
        PromotionEvidenceError,
        match="persisted PROMOTE lacks product-issued effective-sample/dependence authority",
    ):
        ScientificRegistry(legacy_path)

    # The rejected legacy champion image must not establish the first monotonic
    # baseline. Restoring the valid pre-promotion prefix remains admissible.
    legacy_path.write_bytes(valid_bytes)
    reopened = ScientificRegistry(legacy_path)
    assert reopened.get(
        "PromotionDecision", decision.promotion_decision_id
    ) is None
    assert reopened.champion_strategy(as_of=T3) is None
