from __future__ import annotations

import json

import pytest

from autosport.operator_source_config import (
    OperatorSourceConfig,
    OperatorSourceConfigError,
    OperatorSourceResolutionState,
    resolve_operator_source,
)


def test_config_round_trip_is_canonical_and_non_authorizing() -> None:
    config = OperatorSourceConfig("betfair_api")

    restored = OperatorSourceConfig.from_json(config.to_json())

    assert restored == config
    assert restored.runtime_authorized is False
    assert json.loads(config.to_json()) == config.to_canonical_dict()
    assert len(config.integrity_sha256) == 64


@pytest.mark.parametrize(
    "source_id",
    [
        " Betfair",
        "Betfair",
        "betfair ",
        "pkg.mod:factory",
        "../plugin",
        "C:/plugin.py",
        "https://provider.invalid/source",
        "file:///plugin.py",
        "betfair.api",
        "betfаir",  # Cyrillic 'а'.
        "",
        "a" * 65,
    ],
)
def test_source_id_rejects_noncanonical_or_executable_shapes(source_id: str) -> None:
    with pytest.raises(OperatorSourceConfigError):
        OperatorSourceConfig(source_id)


def test_unknown_secret_or_executable_fields_fail_closed_without_echo() -> None:
    base = OperatorSourceConfig("betfair_api").to_canonical_dict()
    for key, secret in (
        ("password", "never-echo-password"),
        ("token", "never-echo-token"),
        ("factory", "pkg.mod:factory"),
        ("path", "C:/secret/plugin.py"),
    ):
        payload = json.dumps({**base, key: secret})
        with pytest.raises(OperatorSourceConfigError) as caught:
            OperatorSourceConfig.from_json(payload)
        assert secret not in str(caught.value)


def test_duplicate_json_keys_fail_closed() -> None:
    config = OperatorSourceConfig("betfair_api")
    payload = (
        '{"schema_version":1,"source_id":"betfair_api",'
        '"source_id":"other","integrity_sha256":"'
        + config.integrity_sha256
        + '"}'
    )

    with pytest.raises(OperatorSourceConfigError, match="duplicate JSON key"):
        OperatorSourceConfig.from_json(payload)


def test_bool_schema_version_is_not_accepted_as_integer_alias() -> None:
    raw = OperatorSourceConfig("betfair_api").to_canonical_dict()
    raw["schema_version"] = True

    with pytest.raises(OperatorSourceConfigError, match="schema_version"):
        OperatorSourceConfig.from_json(json.dumps(raw))


def test_digest_tamper_fails_closed() -> None:
    raw = OperatorSourceConfig("betfair_api").to_canonical_dict()
    raw["source_id"] = "matchbook_api"

    with pytest.raises(OperatorSourceConfigError, match="integrity"):
        OperatorSourceConfig.from_json(json.dumps(raw))


def test_missing_configuration_is_explicit_and_non_authorizing() -> None:
    resolution = resolve_operator_source(None)

    assert resolution.state is OperatorSourceResolutionState.CONFIGURATION_REQUIRED
    assert resolution.source_id is None
    assert resolution.runtime_authorized is False


def test_persisted_identity_resolves_without_granting_runtime_authority() -> None:
    resolution = resolve_operator_source(OperatorSourceConfig("betfair_api"))

    assert resolution.state is OperatorSourceResolutionState.CONFIGURED
    assert resolution.source_id == "betfair_api"
    assert resolution.persisted_source_id == "betfair_api"
    assert resolution.override_source_id is None
    assert resolution.runtime_authorized is False


def test_matching_override_is_explicit_but_not_authority() -> None:
    resolution = resolve_operator_source(
        OperatorSourceConfig("betfair_api"), override_source_id="betfair_api"
    )

    assert resolution.state is OperatorSourceResolutionState.CONFIGURED
    assert resolution.source_id == "betfair_api"
    assert resolution.runtime_authorized is False


def test_conflicting_override_never_uses_implicit_precedence() -> None:
    resolution = resolve_operator_source(
        OperatorSourceConfig("betfair_api"), override_source_id="matchbook_api"
    )

    assert resolution.state is OperatorSourceResolutionState.CONFLICT
    assert resolution.source_id is None
    assert resolution.persisted_source_id == "betfair_api"
    assert resolution.override_source_id == "matchbook_api"
    assert resolution.runtime_authorized is False


def test_override_only_is_configuration_identity_not_execution_authority() -> None:
    resolution = resolve_operator_source(None, override_source_id="matchbook_api")

    assert resolution.state is OperatorSourceResolutionState.CONFIGURED
    assert resolution.source_id == "matchbook_api"
    assert resolution.runtime_authorized is False


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        "null",
        '{"schema_version":NaN,"source_id":"betfair_api","integrity_sha256":"x"}',
        '{"schema_version":Infinity,"source_id":"betfair_api","integrity_sha256":"x"}',
    ],
)
def test_malformed_or_nonfinite_json_fails_closed(payload: str) -> None:
    with pytest.raises(OperatorSourceConfigError):
        OperatorSourceConfig.from_json(payload)
