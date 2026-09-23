from __future__ import annotations

import hashlib
import json

import pytest

from autosport.operator_source_config import (
    CONFIG_SCHEMA,
    CONFIG_VERSION,
    OperatorSourceConfigError,
    OperatorSourceResolutionState,
    dump_operator_source_config,
    load_operator_source_config,
    resolve_operator_source,
    validate_source_id,
)


@pytest.mark.parametrize(
    "source_id",
    [
        "parlayapi:table_tennis",
        "betfair:football",
        "betdaq:horse-racing",
        "smarkets",
        "the-odds-api:ice_hockey",
    ],
)
def test_source_identity_round_trips(source_id: str) -> None:
    payload = dump_operator_source_config(source_id)
    loaded = load_operator_source_config(payload)
    assert loaded.source_id == source_id
    assert loaded.schema == CONFIG_SCHEMA
    assert loaded.version == CONFIG_VERSION


def test_payload_is_deterministic_canonical_json() -> None:
    first = dump_operator_source_config("parlayapi:table_tennis")
    second = dump_operator_source_config("parlayapi:table_tennis")
    assert first == second
    assert first == json.dumps(
        json.loads(first),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_digest_binds_canonical_body_not_runtime_authority() -> None:
    payload = json.loads(dump_operator_source_config("smarkets"))
    body = json.dumps(
        {
            "schema": CONFIG_SCHEMA,
            "source_id": "smarkets",
            "version": CONFIG_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert payload["sha256"] == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize(
    "source_id",
    [
        "",
        " betfair",
        "betfair ",
        "Betfair",
        "BETFAIR",
        "pkg.mod:factory",
        "pkg/mod",
        "../provider",
        r"C:\provider.py",
        "https://provider.invalid",
        "file:provider.py",
        "betfair::football",
        "betfair:",
        ":betfair",
        "betfair/football",
        "betfair football",
        "betfaіr",  # Cyrillic i.
        "a" * 33,
        "a:" + "b" * 33,
        "a:b:c:d:e",
    ],
)
def test_executable_or_ambiguous_source_identity_is_rejected(
    source_id: str,
) -> None:
    with pytest.raises(
        OperatorSourceConfigError, match="invalid operator source identity"
    ):
        validate_source_id(source_id)


@pytest.mark.parametrize("source_id", [None, 1, 1.0, True, [], {}])
def test_non_string_source_identity_is_rejected(source_id: object) -> None:
    with pytest.raises(OperatorSourceConfigError):
        validate_source_id(source_id)


def test_unknown_semantic_identity_is_only_syntax_validated_here() -> None:
    # The product registry, not this payload, owns the allow-list.
    assert validate_source_id("future-provider:sport") == "future-provider:sport"


def test_duplicate_json_key_is_rejected() -> None:
    valid = dump_operator_source_config("smarkets")
    digest = json.loads(valid)["sha256"]
    payload = (
        '{"schema":"autosport.operator-source-config","schema":'
        '"autosport.operator-source-config","sha256":"'
        + digest
        + '","source_id":"smarkets","version":1}'
    )
    with pytest.raises(OperatorSourceConfigError):
        load_operator_source_config(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "version": True},
        lambda value: {**value, "version": 1.0},
        lambda value: {**value, "version": 2},
        lambda value: {**value, "schema": "other"},
        lambda value: {**value, "secret": "do-not-store"},
        lambda value: {key: val for key, val in value.items() if key != "sha256"},
        lambda value: {**value, "sha256": "0" * 64},
        lambda value: {**value, "sha256": "A" * 64},
    ],
)
def test_schema_integrity_drift_is_rejected(mutation) -> None:
    value = json.loads(dump_operator_source_config("smarkets"))
    changed = mutation(value)
    payload = json.dumps(
        changed,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with pytest.raises(OperatorSourceConfigError):
        load_operator_source_config(payload)


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "null",
        "[]",
        "{}",
        '{"schema":NaN}',
        "{",
        " " + dump_operator_source_config("smarkets"),
        dump_operator_source_config("smarkets") + "\n",
    ],
)
def test_malformed_or_noncanonical_payload_is_rejected(payload: str) -> None:
    with pytest.raises(OperatorSourceConfigError):
        load_operator_source_config(payload)


def test_payload_size_is_bounded() -> None:
    with pytest.raises(OperatorSourceConfigError):
        load_operator_source_config("x" * 4097)


def test_tampered_source_id_with_old_digest_is_rejected() -> None:
    value = json.loads(dump_operator_source_config("smarkets"))
    value["source_id"] = "betfair:football"
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with pytest.raises(OperatorSourceConfigError):
        load_operator_source_config(payload)


def test_missing_selection_requires_configuration() -> None:
    resolution = resolve_operator_source(None, None)
    assert resolution.state is OperatorSourceResolutionState.CONFIGURATION_REQUIRED
    assert resolution.source_id is None
    assert resolution.reason_code == "SOURCE_SELECTION_MISSING"
    assert resolution.runtime_authorized is False


def test_persisted_selection_is_selected_but_not_authorized() -> None:
    persisted = dump_operator_source_config("smarkets")
    resolution = resolve_operator_source(persisted, None)
    assert resolution.state is OperatorSourceResolutionState.SELECTED
    assert resolution.source_id == "smarkets"
    assert resolution.persisted_source_id == "smarkets"
    assert resolution.override_source_id is None
    assert resolution.runtime_authorized is False


def test_admin_override_only_is_selected_but_not_authorized() -> None:
    resolution = resolve_operator_source(None, "betfair:football")
    assert resolution.state is OperatorSourceResolutionState.SELECTED
    assert resolution.source_id == "betfair:football"
    assert resolution.runtime_authorized is False


def test_equal_persisted_and_override_authorities_converge() -> None:
    persisted = dump_operator_source_config("smarkets")
    resolution = resolve_operator_source(persisted, "smarkets")
    assert resolution.state is OperatorSourceResolutionState.SELECTED
    assert resolution.source_id == "smarkets"
    assert resolution.persisted_source_id == "smarkets"
    assert resolution.override_source_id == "smarkets"


def test_disagreeing_persisted_and_override_authorities_fail_closed() -> None:
    persisted = dump_operator_source_config("smarkets")
    resolution = resolve_operator_source(persisted, "betfair:football")
    assert resolution.state is OperatorSourceResolutionState.CONFLICT
    assert resolution.source_id is None
    assert resolution.persisted_source_id == "smarkets"
    assert resolution.override_source_id == "betfair:football"
    assert resolution.runtime_authorized is False


def test_invalid_persisted_payload_fails_to_configuration_required() -> None:
    resolution = resolve_operator_source('{"token":"super-secret"}', None)
    assert resolution.state is OperatorSourceResolutionState.CONFIGURATION_REQUIRED
    assert resolution.source_id is None
    assert resolution.reason_code == "PERSISTED_CONFIGURATION_INVALID"
    assert "super-secret" not in repr(resolution)


def test_invalid_override_fails_closed_without_echo() -> None:
    resolution = resolve_operator_source(None, "token=super-secret")
    assert resolution.state is OperatorSourceResolutionState.CONFIGURATION_REQUIRED
    assert resolution.source_id is None
    assert resolution.reason_code == "ADMIN_OVERRIDE_INVALID"
    assert "super-secret" not in repr(resolution)


def test_invalid_override_blocks_even_with_valid_persisted_selection() -> None:
    persisted = dump_operator_source_config("smarkets")
    resolution = resolve_operator_source(persisted, "pkg.mod:factory")
    assert resolution.state is OperatorSourceResolutionState.CONFIGURATION_REQUIRED
    assert resolution.source_id is None
    assert resolution.persisted_source_id == "smarkets"
    assert resolution.runtime_authorized is False


def test_error_message_never_echoes_rejected_identity() -> None:
    secretish = "password=not-for-logs"
    with pytest.raises(OperatorSourceConfigError) as captured:
        validate_source_id(secretish)
    assert secretish not in str(captured.value)
