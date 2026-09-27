"""Strict secret-free operator source-selection configuration.

This module deliberately stores only a stable product source identifier. It does not
resolve Python callables, credentials, or runtime authority; canonical product wiring
must still validate the selected identifier against its closed source registry.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Mapping

_SCHEMA = "autosport.operator-source-config"
_SCHEMA_VERSION = 1
_SOURCE_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z", re.ASCII)
_MAX_SOURCE_ID_LEN = 64


class OperatorSourceConfigError(ValueError):
    """Raised for malformed or non-canonical source configuration."""


class OperatorSourceSelectionState(str, Enum):
    CONFIGURATION_REQUIRED = "configuration_required"
    CONFIGURED = "configured"
    ADMIN_OVERRIDE = "admin_override"
    CONFLICT = "conflict"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class OperatorSourceConfig:
    source_id: str
    integrity_sha256: str

    def __post_init__(self) -> None:
        source_id = _source_id(self.source_id)
        expected = _payload_sha256(source_id)
        if not isinstance(self.integrity_sha256, str) or self.integrity_sha256 != expected:
            raise OperatorSourceConfigError("operator source configuration integrity mismatch")

    def to_json_bytes(self) -> bytes:
        return _canonical_json(
            {
                "schema": _SCHEMA,
                "schema_version": _SCHEMA_VERSION,
                "source_id": self.source_id,
                "integrity_sha256": self.integrity_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class OperatorSourceSelection:
    state: OperatorSourceSelectionState
    source_id: str | None
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, OperatorSourceSelectionState):
            raise TypeError("state must be OperatorSourceSelectionState")
        if self.source_id is not None:
            _source_id(self.source_id)
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise TypeError("reason_code must be non-empty text")
        if self.state in {
            OperatorSourceSelectionState.CONFIGURED,
            OperatorSourceSelectionState.ADMIN_OVERRIDE,
        } and self.source_id is None:
            raise OperatorSourceConfigError("configured selection requires source_id")
        if self.state not in {
            OperatorSourceSelectionState.CONFIGURED,
            OperatorSourceSelectionState.ADMIN_OVERRIDE,
        } and self.source_id is not None:
            raise OperatorSourceConfigError("non-configured selection cannot expose source_id")

    @property
    def runtime_authorized(self) -> bool:
        """Configuration alone never grants runtime/source-factory authority."""
        return False


def build_operator_source_config(source_id: str) -> OperatorSourceConfig:
    canonical = _source_id(source_id)
    return OperatorSourceConfig(canonical, _payload_sha256(canonical))


def parse_operator_source_config(payload: bytes | bytearray | memoryview) -> OperatorSourceConfig:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    raw = bytes(payload)
    if not raw or len(raw) > 4096:
        raise OperatorSourceConfigError("operator source configuration payload size is invalid")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise OperatorSourceConfigError("operator source configuration is not valid UTF-8") from None

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in pairs:
            if key in out:
                raise OperatorSourceConfigError("operator source configuration has duplicate keys")
            out[key] = value
        return out

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=lambda _: (_ for _ in ()).throw(
                OperatorSourceConfigError("operator source configuration contains non-finite JSON")
            ),
        )
    except OperatorSourceConfigError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise OperatorSourceConfigError("operator source configuration JSON is invalid") from None
    if not isinstance(value, dict):
        raise OperatorSourceConfigError("operator source configuration must be a JSON object")
    expected_keys = {"schema", "schema_version", "source_id", "integrity_sha256"}
    if set(value) != expected_keys:
        raise OperatorSourceConfigError("operator source configuration fields are invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != _SCHEMA_VERSION:
        raise OperatorSourceConfigError("operator source configuration schema version is unsupported")
    if value["schema"] != _SCHEMA or type(value["schema"]) is not str:
        raise OperatorSourceConfigError("operator source configuration schema is invalid")
    if type(value["source_id"]) is not str or type(value["integrity_sha256"]) is not str:
        raise OperatorSourceConfigError("operator source configuration field types are invalid")
    config = OperatorSourceConfig(value["source_id"], value["integrity_sha256"])
    if config.to_json_bytes() != raw:
        raise OperatorSourceConfigError("operator source configuration encoding is non-canonical")
    return config


def resolve_operator_source_selection(
    *,
    persisted_payload: bytes | bytearray | memoryview | None,
    admin_override_source_id: str | None,
) -> OperatorSourceSelection:
    """Resolve persisted/admin configuration without granting runtime authority.

    A conflicting persisted and admin override is explicit fail-closed state. Missing
    configuration is a first-run CONFIGURATION_REQUIRED state. Malformed persisted
    bytes are INVALID; their contents are never echoed into reason text.
    """
    persisted: OperatorSourceConfig | None
    if persisted_payload is None:
        persisted = None
    else:
        try:
            persisted = parse_operator_source_config(persisted_payload)
        except (OperatorSourceConfigError, TypeError):
            return OperatorSourceSelection(
                OperatorSourceSelectionState.INVALID,
                None,
                "persisted_config_invalid",
            )

    override: str | None = None
    if admin_override_source_id is not None:
        try:
            override = _source_id(admin_override_source_id)
        except (OperatorSourceConfigError, TypeError):
            return OperatorSourceSelection(
                OperatorSourceSelectionState.INVALID,
                None,
                "admin_override_invalid",
            )

    if persisted is None and override is None:
        return OperatorSourceSelection(
            OperatorSourceSelectionState.CONFIGURATION_REQUIRED,
            None,
            "source_configuration_missing",
        )
    if persisted is None:
        return OperatorSourceSelection(
            OperatorSourceSelectionState.ADMIN_OVERRIDE,
            override,
            "admin_override_only",
        )
    if override is None:
        return OperatorSourceSelection(
            OperatorSourceSelectionState.CONFIGURED,
            persisted.source_id,
            "persisted_config",
        )
    if persisted.source_id != override:
        return OperatorSourceSelection(
            OperatorSourceSelectionState.CONFLICT,
            None,
            "persisted_admin_conflict",
        )
    return OperatorSourceSelection(
        OperatorSourceSelectionState.CONFIGURED,
        persisted.source_id,
        "persisted_admin_agree",
    )


def _source_id(value: object) -> str:
    if type(value) is not str:
        raise TypeError("source_id must be text")
    if not value or len(value) > _MAX_SOURCE_ID_LEN or _SOURCE_ID_RE.fullmatch(value) is None:
        raise OperatorSourceConfigError("source_id is not a canonical product identifier")
    return value


def _payload_sha256(source_id: str) -> str:
    payload = _canonical_json(
        {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "source_id": source_id,
        }
    )
    return sha256(payload).hexdigest()


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
