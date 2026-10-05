from __future__ import annotations

import json
from pathlib import Path

import pytest

from autosport.operator_source_configuration import (
    OperatorSourceConfigurationError,
    load_operator_source_configuration,
    operator_source_configuration_path,
    save_operator_source_configuration,
)
from autosport.operator_source_registry import resolve_product_source_entry


class _StringSubclass(str):
    pass


def test_missing_configuration_is_explicit_first_run_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    assert load_operator_source_configuration(workspace) is None
    assert not operator_source_configuration_path(workspace).exists()


def test_valid_choice_round_trips_through_closed_product_registry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    expected = resolve_product_source_entry("parlayapi-table-tennis")

    saved = save_operator_source_configuration(
        workspace,
        "parlayapi-table-tennis",
    )
    restored = load_operator_source_configuration(workspace)

    assert restored is not None
    assert saved.source_id == expected.source_id
    assert saved.entry is expected
    assert restored.source_id == expected.source_id
    assert restored.entry is expected


def test_persisted_state_contains_identity_not_executable_factory_or_secret(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    save_operator_source_configuration(workspace, "parlayapi-table-tennis")

    raw = json.loads(
        operator_source_configuration_path(workspace).read_text(encoding="utf-8")
    )

    assert set(raw) == {"schema", "schema_version", "source_id", "state_sha256"}
    assert raw["source_id"] == "parlayapi-table-tennis"
    encoded = json.dumps(raw, sort_keys=True)
    assert "create_parlay_product_source" not in encoded
    assert "AUTOSPORT_" not in encoded
    assert "token" not in encoded.lower()
    assert "password" not in encoded.lower()


@pytest.mark.parametrize(
    "source_id",
    [
        "unknown-source",
        "ParlayApi-Table-Tennis",
        "parlayapi-table-tennis ",
        "autosport.product_source:create_parlay_product_source",
    ],
)
def test_save_rejects_non_registry_source_identity(
    tmp_path: Path,
    source_id: str,
) -> None:
    workspace = tmp_path / "workspace"

    with pytest.raises(OperatorSourceConfigurationError):
        save_operator_source_configuration(workspace, source_id)

    assert not operator_source_configuration_path(workspace).exists()


def test_source_id_subclass_is_rejected_before_registry_resolution(tmp_path: Path) -> None:
    with pytest.raises(OperatorSourceConfigurationError, match="exact text"):
        save_operator_source_configuration(
            tmp_path / "workspace",
            _StringSubclass("parlayapi-table-tennis"),
        )


def test_workspace_string_subclass_is_rejected_before_path_hooks() -> None:
    with pytest.raises(OperatorSourceConfigurationError, match="exact str or exact Path"):
        operator_source_configuration_path(_StringSubclass("workspace"))


def test_unknown_stored_identity_fails_closed_after_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    save_operator_source_configuration(workspace, "parlayapi-table-tennis")
    path = operator_source_configuration_path(workspace)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["source_id"] = "unknown-source"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(OperatorSourceConfigurationError):
        load_operator_source_configuration(workspace)


def test_changed_identity_without_integrity_update_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    save_operator_source_configuration(workspace, "parlayapi-table-tennis")
    path = operator_source_configuration_path(workspace)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("parlayapi-table-tennis", "unknown-source"),
        encoding="utf-8",
    )

    with pytest.raises(
        OperatorSourceConfigurationError,
        match="integrity check failed",
    ):
        load_operator_source_configuration(workspace)


def test_schema_version_tamper_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    save_operator_source_configuration(workspace, "parlayapi-table-tennis")
    path = operator_source_configuration_path(workspace)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 2
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(
        OperatorSourceConfigurationError,
        match="version is unsupported",
    ):
        load_operator_source_configuration(workspace)


def test_duplicate_json_key_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    path = operator_source_configuration_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            '{"schema":"autosport.operator-source-configuration",'
            '"schema_version":1,'
            '"source_id":"parlayapi-table-tennis",'
            '"source_id":"parlayapi-table-tennis",'
            '"state_sha256":"' + ("0" * 64) + '"}'
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        OperatorSourceConfigurationError,
        match="cannot verify persisted",
    ):
        load_operator_source_configuration(workspace)


def test_successive_valid_save_replaces_choice_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first = save_operator_source_configuration(workspace, "parlayapi-table-tennis")
    second = save_operator_source_configuration(workspace, "parlayapi-table-tennis")

    assert first.source_id == second.source_id
    assert load_operator_source_configuration(workspace) == second
