from __future__ import annotations

import json

import pytest

import autosport.operator_source_config as operator_source_config
from autosport.operator_source_config import (
    OperatorSourceConfigError,
    OperatorSourceSelectionState,
    build_operator_source_config,
    parse_operator_source_config,
    resolve_operator_source_selection,
)


def test_round_trip_is_canonical_and_restart_stable():
    record = build_operator_source_config("betfair-exchange")
    payload = record.to_json_bytes()
    assert parse_operator_source_config(payload) == record
    assert parse_operator_source_config(payload).to_json_bytes() == payload


@pytest.mark.parametrize(
    "value",
    [
        " pkg",
        "pkg ",
        "Pkg",
        "pkg.mod:factory",
        "pkg.mod",
        "../pkg",
        "C:/pkg",
        "file://pkg",
        "https://pkg",
        "бетfair",
        "betfаir",  # Cyrillic a
        "a_b",
        "a--b",
        "-a",
        "a-",
        "",
        "a" * 65,
    ],
)
def test_source_id_rejects_executable_ambiguous_or_confusable_text(value):
    with pytest.raises((OperatorSourceConfigError, TypeError)):
        build_operator_source_config(value)


def test_integrity_tamper_fails_closed():
    payload = build_operator_source_config("betfair-exchange").to_json_bytes()
    obj = json.loads(payload)
    obj["source_id"] = "paper-fixture"
    tampered = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(OperatorSourceConfigError, match="integrity mismatch"):
        parse_operator_source_config(tampered)


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", None, 2])
def test_schema_version_requires_exact_integer_one(schema_version):
    obj = json.loads(build_operator_source_config("betfair-exchange").to_json_bytes())
    obj["schema_version"] = schema_version
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(OperatorSourceConfigError, match="schema version"):
        parse_operator_source_config(payload)


def test_duplicate_keys_are_rejected():
    payload = (
        b'{"integrity_sha256":"x","schema":"autosport.operator-source-config",'
        b'"schema_version":1,"source_id":"a","source_id":"b"}'
    )
    with pytest.raises(OperatorSourceConfigError, match="duplicate keys"):
        parse_operator_source_config(payload)


@pytest.mark.parametrize("extra_key", ["token", "password", "application_key", "factory", "module", "callable"])
def test_unknown_or_secret_executable_fields_are_rejected_without_echo(extra_key):
    obj = json.loads(build_operator_source_config("betfair-exchange").to_json_bytes())
    obj[extra_key] = "SUPER-SECRET-value"
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(OperatorSourceConfigError) as exc:
        parse_operator_source_config(payload)
    assert "SUPER-SECRET" not in str(exc.value)


def test_noncanonical_json_encoding_is_rejected():
    obj = json.loads(build_operator_source_config("betfair-exchange").to_json_bytes())
    payload = json.dumps(obj, sort_keys=False, indent=2).encode()
    with pytest.raises(OperatorSourceConfigError, match="non-canonical"):
        parse_operator_source_config(payload)


def test_recursive_json_decoder_failure_is_normalized_and_invalid(monkeypatch):
    payload = build_operator_source_config("betfair-exchange").to_json_bytes()

    def raise_recursion(*args, **kwargs):
        raise RecursionError("decoder recursion limit exceeded")

    monkeypatch.setattr(operator_source_config.json, "loads", raise_recursion)

    with pytest.raises(OperatorSourceConfigError, match="JSON is invalid"):
        parse_operator_source_config(payload)

    result = resolve_operator_source_selection(
        persisted_payload=payload,
        admin_override_source_id=None,
    )
    assert result.state is OperatorSourceSelectionState.INVALID
    assert result.source_id is None
    assert result.reason_code == "persisted_config_invalid"
    assert result.runtime_authorized is False


def test_bounded_deep_json_never_escapes_first_run_recovery():
    depth = 1024
    payload = b"[" * depth + b"0" + b"]" * depth
    assert len(payload) < 4096

    result = resolve_operator_source_selection(
        persisted_payload=payload,
        admin_override_source_id=None,
    )
    assert result.state is OperatorSourceSelectionState.INVALID
    assert result.source_id is None
    assert result.runtime_authorized is False


def test_missing_config_is_explicit_first_run_state():
    result = resolve_operator_source_selection(persisted_payload=None, admin_override_source_id=None)
    assert result.state is OperatorSourceSelectionState.CONFIGURATION_REQUIRED
    assert result.source_id is None
    assert result.runtime_authorized is False


def test_persisted_config_is_visible_but_never_runtime_authority():
    payload = build_operator_source_config("betfair-exchange").to_json_bytes()
    result = resolve_operator_source_selection(persisted_payload=payload, admin_override_source_id=None)
    assert result.state is OperatorSourceSelectionState.CONFIGURED
    assert result.source_id == "betfair-exchange"
    assert result.runtime_authorized is False


def test_admin_override_only_is_explicit_and_not_runtime_authority():
    result = resolve_operator_source_selection(
        persisted_payload=None,
        admin_override_source_id="paper-fixture",
    )
    assert result.state is OperatorSourceSelectionState.ADMIN_OVERRIDE
    assert result.source_id == "paper-fixture"
    assert result.runtime_authorized is False


def test_matching_persisted_and_override_converge():
    payload = build_operator_source_config("betfair-exchange").to_json_bytes()
    result = resolve_operator_source_selection(
        persisted_payload=payload,
        admin_override_source_id="betfair-exchange",
    )
    assert result.state is OperatorSourceSelectionState.CONFIGURED
    assert result.source_id == "betfair-exchange"
    assert result.reason_code == "persisted_admin_agree"


def test_conflicting_persisted_and_override_fail_closed():
    payload = build_operator_source_config("betfair-exchange").to_json_bytes()
    result = resolve_operator_source_selection(
        persisted_payload=payload,
        admin_override_source_id="paper-fixture",
    )
    assert result.state is OperatorSourceSelectionState.CONFLICT
    assert result.source_id is None
    assert result.runtime_authorized is False


def test_invalid_persisted_payload_does_not_echo_secret():
    secret = b'{"token":"abc-SUPER-SECRET-xyz"}'
    result = resolve_operator_source_selection(
        persisted_payload=secret,
        admin_override_source_id=None,
    )
    assert result.state is OperatorSourceSelectionState.INVALID
    assert "SUPER-SECRET" not in result.reason_code


def test_invalid_override_is_invalid_not_configuration_required():
    result = resolve_operator_source_selection(
        persisted_payload=None,
        admin_override_source_id="pkg.mod:factory",
    )
    assert result.state is OperatorSourceSelectionState.INVALID
    assert result.source_id is None


def test_payload_size_is_bounded():
    with pytest.raises(OperatorSourceConfigError, match="size"):
        parse_operator_source_config(b"x" * 4097)
