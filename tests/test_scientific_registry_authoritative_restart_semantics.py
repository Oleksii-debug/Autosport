from __future__ import annotations

from dataclasses import replace
import json

import pytest

from autosport.scientific_registry import (
    DuplicateExperimentFingerprintError,
    ScientificRegistry,
)
from test_scientific_registry import T3, _experiment, _foundation


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
