from __future__ import annotations

import hashlib
import json

import pytest

from autosport._predictive_target_protocol_envelope_authority import (
    _require_bound_protocol_entry,
)
from autosport.predictive_target_semantics import PredictiveTargetSemanticsError
from autosport.reproducibility_manifest import (
    FactoryReproducibilityManifest,
    WalkForwardSplit,
)
from autosport.scientific_registry import ScientificRegistry


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:01:00+00:00"
T2 = "2026-01-02T00:00:00+00:00"
T3 = "2026-01-03T00:00:00+00:00"


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _entry(*, available_at: str) -> dict[str, object]:
    # The payload is deliberately invariant. Only the immutable registry envelope
    # availability changes between the authentic and copied histories.
    payload = {
        "research_protocol_id": "protocol-envelope-v1",
        "protocol_sha256": SHA_A,
        "binding": {"kind": "frozen-protocol-payload"},
    }
    raw: dict[str, object] = {
        "record_type": "ResearchProtocol",
        "record_id": "protocol-envelope-v1",
        "available_at": available_at,
        "payload": payload,
    }
    raw["record_sha256"] = _digest(raw)
    return raw


def _registry(path, raw: dict[str, object]) -> ScientificRegistry:
    path.write_text(
        json.dumps(
            {"schema_version": 1, "records": [raw]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    return ScientificRegistry(path)


def _manifest(authentic: dict[str, object]) -> FactoryReproducibilityManifest:
    return FactoryReproducibilityManifest(
        experiment_id="experiment-envelope-v1",
        evaluation_bundle_id="evaluation-envelope-v1",
        dataset_snapshot_id="dataset-envelope-v1",
        dataset_manifest_sha256=SHA_B,
        training_points_manifest_sha256=SHA_B,
        dataset_source_identity="lawful-provider:fixture",
        dataset_license_identity="license:v1",
        splits=(
            WalkForwardSplit(
                fold_id="fold-1",
                training_indices=(0,),
                evaluation_index=1,
                training_cutoff=T2,
                evaluation_at=T3,
            ),
        ),
        model_version_id="model-envelope-v1",
        model_artifact_sha256=SHA_C,
        learner_state_sha256=SHA_D,
        model_config_sha256=SHA_C,
        research_protocol_id="protocol-envelope-v1",
        protocol_sha256=SHA_A,
        evaluator_config_sha256=SHA_D,
        source_sha256=SHA_A,
        evaluator_source_sha256=SHA_B,
        environment_sha256=SHA_C,
        seed=17,
        research_protocol_record_sha256=authentic["record_sha256"],
        research_protocol_available_at=authentic["available_at"],
    )


def test_same_payload_backdated_protocol_envelope_cannot_reuse_manifest(tmp_path) -> None:
    authentic = _entry(available_at=T1)
    copied = _entry(available_at=T0)

    assert authentic["payload"] == copied["payload"]
    assert authentic["record_sha256"] != copied["record_sha256"]

    manifest = _manifest(authentic)
    authentic_registry = _registry(tmp_path / "authentic.json", authentic)
    copied_registry = _registry(tmp_path / "copied.json", copied)

    resolved = _require_bound_protocol_entry(authentic_registry, manifest)
    assert resolved["record_sha256"] == authentic["record_sha256"]

    with pytest.raises(
        PredictiveTargetSemanticsError,
        match="registry record identity does not match reproducibility manifest",
    ):
        _require_bound_protocol_entry(copied_registry, manifest)
