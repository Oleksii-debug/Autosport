"""Strict, non-executable operator source-selection configuration.

This module persists only a product-owned machine source identity.  It deliberately
contains no factory, import path, credential, provider client, or runtime authority;
the canonical runtime composition layer must independently resolve and authorize the
selected identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Final


_SCHEMA_VERSION: Final[int] = 1
_SOURCE_ID_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z", re.ASCII)
_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {"schema_version", "source_id", "integrity_sha256"}
)


class OperatorSourceConfigError(ValueError):
    """Raised when operator source configuration is malformed or contradictory."""


class OperatorSourceResolutionState(str, Enum):
    CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
    CONFIGURED = "CONFIGURED"
    CONFLICT = "CONFLICT"


def _source_id(value: object, field: str = "source_id") -> str:
    if not isinstance(value, str):
        raise OperatorSourceConfigError(f"{field} must be text")
    if value != value.strip() or not _SOURCE_ID_RE.fullmatch(value):
        raise OperatorSourceConfigError(
            f"{field} must be a canonical lowercase ASCII product source id"
        )
    return value


def _canonical_body(source_id: str) -> bytes:
    return json.dumps(
        {"schema_version": _SCHEMA_VERSION, "source_id": source_id},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _integrity(source_id: str) -> str:
    # Integrity/canonicalization only.  This is not authentication or authority.
    return sha256(_canonical_body(source_id)).hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OperatorSourceConfigError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class OperatorSourceConfig:
    """Secret-free persisted source identity with deterministic integrity evidence."""

    source_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _source_id(self.source_id))

    @property
    def runtime_authorized(self) -> bool:
        """Configuration never grants executable/runtime authority."""

        return False

    @property
    def integrity_sha256(self) -> str:
        return _integrity(self.source_id)

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "source_id": self.source_id,
            "integrity_sha256": self.integrity_sha256,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> "OperatorSourceConfig":
        if not isinstance(payload, (str, bytes)):
            raise OperatorSourceConfigError("config payload must be JSON text or bytes")
        try:
            raw = json.loads(
                payload,
                object_pairs_hook=_strict_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    OperatorSourceConfigError(f"invalid JSON constant: {value}")
                ),
            )
        except OperatorSourceConfigError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise OperatorSourceConfigError("config payload is not strict JSON") from exc

        if not isinstance(raw, dict) or frozenset(raw) != _PAYLOAD_KEYS:
            raise OperatorSourceConfigError("config payload schema is invalid")
        version = raw["schema_version"]
        if type(version) is not int or version != _SCHEMA_VERSION:
            raise OperatorSourceConfigError("unsupported config schema_version")
        source_id = _source_id(raw["source_id"])
        digest = raw["integrity_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise OperatorSourceConfigError("integrity_sha256 must be lowercase SHA-256 text")
        expected = _integrity(source_id)
        if digest != expected:
            raise OperatorSourceConfigError("config integrity check failed")
        return cls(source_id=source_id)


@dataclass(frozen=True, slots=True)
class OperatorSourceResolution:
    state: OperatorSourceResolutionState
    source_id: str | None
    persisted_source_id: str | None
    override_source_id: str | None

    @property
    def runtime_authorized(self) -> bool:
        return False


def resolve_operator_source(
    persisted: OperatorSourceConfig | None,
    *,
    override_source_id: str | None = None,
) -> OperatorSourceResolution:
    """Resolve config identity without deciding whether that identity may execute.

    A disagreement is explicit rather than relying on read order or silently choosing
    a precedence rule.  The runtime layer must separately validate the selected ID
    against its closed product-owned registry before constructing any source.
    """

    if persisted is not None and not isinstance(persisted, OperatorSourceConfig):
        raise OperatorSourceConfigError("persisted config has the wrong type")
    override = None if override_source_id is None else _source_id(override_source_id, "override_source_id")
    stored = None if persisted is None else persisted.source_id

    if stored is None and override is None:
        return OperatorSourceResolution(
            state=OperatorSourceResolutionState.CONFIGURATION_REQUIRED,
            source_id=None,
            persisted_source_id=None,
            override_source_id=None,
        )
    if stored is not None and override is not None and stored != override:
        return OperatorSourceResolution(
            state=OperatorSourceResolutionState.CONFLICT,
            source_id=None,
            persisted_source_id=stored,
            override_source_id=override,
        )
    selected = stored if stored is not None else override
    return OperatorSourceResolution(
        state=OperatorSourceResolutionState.CONFIGURED,
        source_id=selected,
        persisted_source_id=stored,
        override_source_id=override,
    )
