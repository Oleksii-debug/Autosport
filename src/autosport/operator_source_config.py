from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import hmac
import json
import re
from typing import Any

CONFIG_SCHEMA = "autosport.operator-source-config"
CONFIG_VERSION = 1
_MAX_PAYLOAD_BYTES = 4096
_SOURCE_ID_RE = re.compile(
    r"[a-z0-9]+(?:-[a-z0-9]+)*\Z",
    re.ASCII,
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_REQUIRED_KEYS = frozenset({"schema", "version", "source_id", "sha256"})


class OperatorSourceConfigError(ValueError):
    """Raised when persisted operator source configuration is not canonical."""


class OperatorSourceResolutionState(StrEnum):
    CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
    SELECTED = "SELECTED"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class OperatorSourceConfig:
    source_id: str
    schema: str = CONFIG_SCHEMA
    version: int = CONFIG_VERSION


@dataclass(frozen=True, slots=True)
class OperatorSourceResolution:
    state: OperatorSourceResolutionState
    source_id: str | None
    persisted_source_id: str | None
    override_source_id: str | None
    reason_code: str

    @property
    def runtime_authorized(self) -> bool:
        """Configuration selection is never runtime/provider authority."""
        return False


def validate_source_id(value: object) -> str:
    """Return one strict, language-neutral product source identity."""
    if (
        type(value) is not str
        or len(value) > 64
        or _SOURCE_ID_RE.fullmatch(value) is None
    ):
        raise OperatorSourceConfigError("invalid operator source identity")
    return value


def _canonical_body(source_id: str) -> dict[str, object]:
    return {
        "schema": CONFIG_SCHEMA,
        "source_id": source_id,
        "version": CONFIG_VERSION,
    }


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _body_digest(source_id: str) -> str:
    body = _canonical_json(_canonical_body(source_id)).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def dump_operator_source_config(source_id: str) -> str:
    """Serialize a strict integrity-tagged operator selection.

    The digest detects accidental/tampered bytes. It is deliberately not an
    authentication token and does not authorize a runtime or provider.
    """
    source_id = validate_source_id(source_id)
    payload = {
        **_canonical_body(source_id),
        "sha256": _body_digest(source_id),
    }
    return _canonical_json(payload)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OperatorSourceConfigError(
                "invalid operator source configuration"
            )
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> None:
    raise OperatorSourceConfigError("invalid operator source configuration")


def load_operator_source_config(payload: str) -> OperatorSourceConfig:
    """Parse persisted source selection with exact schema/integrity checks."""
    if type(payload) is not str:
        raise OperatorSourceConfigError("invalid operator source configuration")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeError as exc:
        raise OperatorSourceConfigError(
            "invalid operator source configuration"
        ) from exc
    if not encoded or len(encoded) > _MAX_PAYLOAD_BYTES:
        raise OperatorSourceConfigError("invalid operator source configuration")
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except OperatorSourceConfigError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OperatorSourceConfigError(
            "invalid operator source configuration"
        ) from exc

    if type(decoded) is not dict or frozenset(decoded) != _REQUIRED_KEYS:
        raise OperatorSourceConfigError("invalid operator source configuration")
    if decoded.get("schema") != CONFIG_SCHEMA:
        raise OperatorSourceConfigError("invalid operator source configuration")
    if type(decoded.get("version")) is not int:
        raise OperatorSourceConfigError("invalid operator source configuration")
    if decoded["version"] != CONFIG_VERSION:
        raise OperatorSourceConfigError("invalid operator source configuration")

    source_id = validate_source_id(decoded.get("source_id"))
    digest = decoded.get("sha256")
    if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
        raise OperatorSourceConfigError("invalid operator source configuration")
    if not hmac.compare_digest(digest, _body_digest(source_id)):
        raise OperatorSourceConfigError("invalid operator source configuration")

    # Canonical representation is part of the persisted contract. This rejects
    # alternate whitespace/key-order encodings that could hide byte drift.
    if payload != dump_operator_source_config(source_id):
        raise OperatorSourceConfigError("invalid operator source configuration")
    return OperatorSourceConfig(source_id=source_id)


def resolve_operator_source(
    persisted_payload: str | None,
    admin_override_source_id: str | None,
) -> OperatorSourceResolution:
    """Resolve configuration without minting runtime/provider authority."""
    persisted_source_id: str | None = None
    override_source_id: str | None = None

    if persisted_payload is not None:
        try:
            persisted_source_id = load_operator_source_config(
                persisted_payload
            ).source_id
        except OperatorSourceConfigError:
            return OperatorSourceResolution(
                state=OperatorSourceResolutionState.CONFIGURATION_REQUIRED,
                source_id=None,
                persisted_source_id=None,
                override_source_id=None,
                reason_code="PERSISTED_CONFIGURATION_INVALID",
            )

    if admin_override_source_id is not None:
        try:
            override_source_id = validate_source_id(admin_override_source_id)
        except OperatorSourceConfigError:
            return OperatorSourceResolution(
                state=OperatorSourceResolutionState.CONFIGURATION_REQUIRED,
                source_id=None,
                persisted_source_id=persisted_source_id,
                override_source_id=None,
                reason_code="ADMIN_OVERRIDE_INVALID",
            )

    if persisted_source_id is None and override_source_id is None:
        return OperatorSourceResolution(
            state=OperatorSourceResolutionState.CONFIGURATION_REQUIRED,
            source_id=None,
            persisted_source_id=None,
            override_source_id=None,
            reason_code="SOURCE_SELECTION_MISSING",
        )

    if (
        persisted_source_id is not None
        and override_source_id is not None
        and persisted_source_id != override_source_id
    ):
        return OperatorSourceResolution(
            state=OperatorSourceResolutionState.CONFLICT,
            source_id=None,
            persisted_source_id=persisted_source_id,
            override_source_id=override_source_id,
            reason_code="PERSISTED_OVERRIDE_CONFLICT",
        )

    selected = override_source_id or persisted_source_id
    return OperatorSourceResolution(
        state=OperatorSourceResolutionState.SELECTED,
        source_id=selected,
        persisted_source_id=persisted_source_id,
        override_source_id=override_source_id,
        reason_code="SOURCE_SELECTION_PRESENT",
    )


__all__ = [
    "CONFIG_SCHEMA",
    "CONFIG_VERSION",
    "OperatorSourceConfig",
    "OperatorSourceConfigError",
    "OperatorSourceResolution",
    "OperatorSourceResolutionState",
    "dump_operator_source_config",
    "load_operator_source_config",
    "resolve_operator_source",
    "validate_source_id",
]
