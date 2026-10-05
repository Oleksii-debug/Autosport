from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .operator_source_registry import (
    OperatorSourceRegistryError,
    ProductSourceRegistryEntry,
    resolve_product_source_entry,
)


_SCHEMA = "autosport.operator-source-configuration"
_SCHEMA_VERSION = 1
_STATE_FIELDS = frozenset({"schema", "schema_version", "source_id", "state_sha256"})


class OperatorSourceConfigurationError(RuntimeError):
    """Persisted operator source selection is missing canonical product truth."""


@dataclass(frozen=True, slots=True)
class OperatorSourceConfiguration:
    source_id: str
    entry: ProductSourceRegistryEntry

    def __post_init__(self) -> None:
        if type(self.source_id) is not str:
            raise OperatorSourceConfigurationError("operator source_id must be exact text")
        try:
            canonical = resolve_product_source_entry(self.source_id)
        except OperatorSourceRegistryError as exc:
            raise OperatorSourceConfigurationError(
                "operator source_id is not registered by this product build"
            ) from exc
        if type(self.entry) is not ProductSourceRegistryEntry or self.entry is not canonical:
            raise OperatorSourceConfigurationError(
                "operator source configuration entry is not canonical registry truth"
            )


def _workspace(value: object) -> Path:
    if type(value) not in {str, type(Path("."))}:
        raise OperatorSourceConfigurationError(
            "operator source workspace must be exact str or exact Path"
        )
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise OperatorSourceConfigurationError(
                "operator source workspace must be absolute"
            )
        resolved = candidate.resolve(strict=False)
    except OperatorSourceConfigurationError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise OperatorSourceConfigurationError(
            "operator source workspace cannot be resolved"
        ) from exc
    return resolved


def operator_source_configuration_path(workspace: str | Path) -> Path:
    return _workspace(workspace) / ".autosport" / "operator-source.json"


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OperatorSourceConfigurationError(
            "operator source configuration is not canonical JSON"
        ) from exc


def _state_digest(raw: dict[str, object]) -> str:
    bare = {key: value for key, value in raw.items() if key != "state_sha256"}
    return hashlib.sha256(_canonical_json(bare).encode("utf-8")).hexdigest()


def _sealed_state(source_id: str) -> dict[str, object]:
    raw: dict[str, object] = {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "source_id": source_id,
    }
    raw["state_sha256"] = _state_digest(raw)
    return raw


def _decode_state(raw: object) -> OperatorSourceConfiguration:
    if type(raw) is not dict or not all(type(key) is str for key in raw):
        raise OperatorSourceConfigurationError(
            "operator source configuration must be a JSON object"
        )
    state: dict[str, object] = raw
    if set(state) != _STATE_FIELDS:
        raise OperatorSourceConfigurationError(
            "operator source configuration schema fields do not match"
        )
    if type(state["schema"]) is not str or state["schema"] != _SCHEMA:
        raise OperatorSourceConfigurationError(
            "operator source configuration schema is unsupported"
        )
    if (
        type(state["schema_version"]) is not int
        or state["schema_version"] != _SCHEMA_VERSION
    ):
        raise OperatorSourceConfigurationError(
            "operator source configuration version is unsupported"
        )
    source_id = state["source_id"]
    digest = state["state_sha256"]
    if type(source_id) is not str or type(digest) is not str:
        raise OperatorSourceConfigurationError(
            "operator source configuration scalar types are invalid"
        )
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in "0123456789abcdef" for character in digest)
        or digest != _state_digest(state)
    ):
        raise OperatorSourceConfigurationError(
            "operator source configuration integrity check failed"
        )
    try:
        entry = resolve_product_source_entry(source_id)
    except OperatorSourceRegistryError as exc:
        raise OperatorSourceConfigurationError(
            "configured source is not available in this product build"
        ) from exc
    return OperatorSourceConfiguration(source_id=source_id, entry=entry)


def load_operator_source_configuration(
    workspace: str | Path,
) -> OperatorSourceConfiguration | None:
    path = operator_source_configuration_path(workspace)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        raw = strict_json_loads(text)
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        raise OperatorSourceConfigurationError(
            "cannot verify persisted operator source configuration"
        ) from exc
    return _decode_state(raw)


def save_operator_source_configuration(
    workspace: str | Path,
    source_id: str,
) -> OperatorSourceConfiguration:
    if type(source_id) is not str:
        raise OperatorSourceConfigurationError("operator source_id must be exact text")
    try:
        entry = resolve_product_source_entry(source_id)
    except OperatorSourceRegistryError as exc:
        raise OperatorSourceConfigurationError(
            "operator source_id is not registered by this product build"
        ) from exc

    path = operator_source_configuration_path(workspace)
    payload = _sealed_state(entry.source_id)
    try:
        atomic_write_json(path, payload)
    except (OSError, TypeError, ValueError) as exc:
        raise OperatorSourceConfigurationError(
            "cannot persist operator source configuration"
        ) from exc

    restored = load_operator_source_configuration(workspace)
    if restored is None or restored.entry is not entry:
        raise OperatorSourceConfigurationError(
            "operator source configuration failed exact durable readback"
        )
    return restored
