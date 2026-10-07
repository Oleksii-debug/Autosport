from __future__ import annotations

import json

import pytest

import autosport.scientific_registry as registry_module
from autosport.scientific_registry import (
    DatasetSnapshot,
    ModelVersion,
    ScientificRegistry,
    StrategyVersion,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-10-07T00:00:00+00:00"


def _model(**overrides: object) -> ModelVersion:
    values: dict[str, object] = {
        "model_version_id": "model-v1",
        "model_family": "fixture-model",
        "artifact_sha256": SHA_A,
        "source_sha256": SHA_B,
        "environment_sha256": SHA_C,
        "dataset_snapshot_id": "dataset-v1",
        "feature_set_id": "features-v1",
        "research_protocol_id": "protocol-v1",
        "seed": 7,
        "config_sha256": SHA_D,
        "created_at": T0,
    }
    values.update(overrides)
    return ModelVersion(**values)  # type: ignore[arg-type]


def _strategy(**overrides: object) -> StrategyVersion:
    values: dict[str, object] = {
        "strategy_version_id": "strategy-v1",
        "canonical_strategy_id": "canonical-strategy",
        "source_sha256": SHA_A,
        "environment_sha256": SHA_B,
        "config_sha256": SHA_C,
        "created_at": T0,
        "model_version_id": "model-v1",
    }
    values.update(overrides)
    return StrategyVersion(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "factory"),
    (
        ("artifact_sha256", _model),
        ("source_sha256", _model),
        ("environment_sha256", _model),
        ("config_sha256", _model),
        ("source_sha256", _strategy),
        ("environment_sha256", _strategy),
        ("config_sha256", _strategy),
    ),
)
def test_version_records_reject_uppercase_sha_identity_alias(
    field_name: str,
    factory,
) -> None:
    with pytest.raises(ValueError, match="canonical lowercase SHA-256"):
        factory(**{field_name: "A" * 64})


@pytest.mark.parametrize(
    ("factory", "field_name"),
    (
        (_model, "artifact_sha256"),
        (_strategy, "source_sha256"),
    ),
)
def test_registry_append_revalidates_tampered_version_sha_identity(
    tmp_path,
    factory,
    field_name: str,
) -> None:
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    record = factory()
    object.__setattr__(record, field_name, "A" * 64)

    with pytest.raises(ValueError, match="canonical lowercase SHA-256"):
        registry.append(record)

    assert registry.get(record.record_type, record.record_id) is None


def test_valid_version_sha_payloads_remain_byte_spelling_stable() -> None:
    model = _model()
    strategy = _strategy()

    assert model.to_payload()["artifact_sha256"] == SHA_A
    assert model.to_payload()["source_sha256"] == SHA_B
    assert model.to_payload()["environment_sha256"] == SHA_C
    assert model.to_payload()["config_sha256"] == SHA_D
    assert strategy.to_payload()["source_sha256"] == SHA_A
    assert strategy.to_payload()["environment_sha256"] == SHA_B
    assert strategy.to_payload()["config_sha256"] == SHA_C


def _dataset(cls=DatasetSnapshot, **overrides: object) -> DatasetSnapshot:
    values: dict[str, object] = {
        "dataset_snapshot_id": "dataset-v1",
        "manifest_sha256": SHA_A,
        "source_identity": "source-v1",
        "license_identity": "license-v1",
        "causal_cutoff": T0,
        "available_at_utc": T0,
        "outcome_reveal_after": None,
    }
    values.update(overrides)
    return cls(**values)  # type: ignore[arg-type]


class _DatasetSnapshotSubclass(DatasetSnapshot):
    pass


def test_registry_append_rejects_scientific_record_subclass_before_payload_dispatch(
    tmp_path,
) -> None:
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )

    with pytest.raises(ValueError, match="exact canonical record type"):
        registry.append(_dataset(_DatasetSnapshotSubclass))


def test_registry_append_revalidates_tampered_dataset_snapshot_identity(
    tmp_path,
) -> None:
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    record = _dataset()
    object.__setattr__(record, "manifest_sha256", "A" * 64)

    with pytest.raises(ValueError, match="canonical lowercase SHA-256"):
        registry.append(record)

    assert registry.get("DatasetSnapshot", "dataset-v1") is None


def test_registry_restart_rejects_self_consistent_record_id_payload_mismatch(
    tmp_path,
) -> None:
    path = tmp_path / "scientific-registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    registry.append(_dataset())

    state = json.loads(path.read_text(encoding="utf-8"))
    entry = state["records"][0]
    entry["payload"]["dataset_snapshot_id"] = "dataset-forged"
    entry["record_sha256"] = registry_module._digest(
        {
            "record_type": entry["record_type"],
            "record_id": entry["record_id"],
            "available_at": entry["available_at"],
            "payload": entry["payload"],
        }
    )
    path.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    reopened = ScientificRegistry(path)
    with pytest.raises(ValueError, match="record identity mismatch"):
        reopened.get("DatasetSnapshot", "dataset-v1")


@pytest.mark.parametrize(
    ("factory", "field_name", "value"),
    (
        (_model, "model_version_id", "model-v1\nforged"),
        (_model, "dataset_snapshot_id", "dataset-v1\tforged"),
        (_strategy, "strategy_version_id", "strategy-v1\rforged"),
        (_strategy, "canonical_strategy_id", "canonical-strategy\x7fforged"),
        (_dataset, "source_identity", "source-v1\nforged"),
    ),
)
def test_scientific_registry_text_identity_rejects_control_aliases(factory, field_name: str, value: str) -> None:
    with pytest.raises(ValueError, match="canonical string"):
        factory(**{field_name: value})



class _ForgedExternalRecord:
    def __init__(self, record_type: str) -> None:
        self.record_type = record_type
        self.record_id = "forged-record"
        self.available_at = T0

    def to_payload(self) -> dict[str, object]:
        return {"forged": True}


@pytest.mark.parametrize(
    "record_type",
    (
        "DriftReference",
        "DriftObservation",
        "DriftFinding",
        "PairedVOCEvaluation",
        "VOCCohort",
        "ChampionEligibilityDecision",
    ),
)
def test_registry_rejects_protocol_only_external_record_forgery(
    tmp_path,
    record_type: str,
) -> None:
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )

    with pytest.raises(ValueError, match="canonical record class"):
        registry.append(_ForgedExternalRecord(record_type))  # type: ignore[arg-type]

    assert registry.get(record_type, "forged-record") is None



def test_declared_registry_types_have_one_canonical_write_class() -> None:
    canonical = {
        **registry_module._LOCAL_SCIENTIFIC_RECORD_TYPES,
        **registry_module._external_scientific_record_types(),
    }

    assert set(canonical) == set(registry_module._RECORD_TYPES)
    assert len(set(canonical.values())) == len(canonical)


class _HostileUnknownScientificRecord:
    @property
    def record_type(self):
        raise AssertionError("unknown record_type dispatch must not execute")


def test_registry_rejects_unknown_record_before_record_type_dispatch(tmp_path) -> None:
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )

    with pytest.raises(ValueError, match="exact canonical record type"):
        registry.append(_HostileUnknownScientificRecord())  # type: ignore[arg-type]
