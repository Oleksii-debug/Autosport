"""Crash-safe persisted operator source selection over canonical integrity primitives."""
from __future__ import annotations

import json
from pathlib import Path

from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads
from .operator_source_config import (
    OperatorSourceConfig,
    OperatorSourceConfigError,
    OperatorSourceSelection,
    OperatorSourceSelectionState,
    build_operator_source_config,
    parse_operator_source_config,
    resolve_operator_source_selection,
)

_MAX_PERSISTED_BYTES = 4096
_EXPECTED_KEYS = frozenset(
    {"schema", "schema_version", "source_id", "integrity_sha256"}
)


class OperatorSourceStoreError(RuntimeError):
    """Persisted operator source configuration cannot be trusted or published."""


class OperatorSourceConfigStore:
    """One durable source-id preference; never a runtime/source-factory authority."""

    def __init__(self, path: str | Path) -> None:
        if not isinstance(path, (str, Path)):
            raise TypeError("path must be str or Path")
        self.path = Path(path)

    def read(self) -> OperatorSourceConfig | None:
        """Return the exact valid stored config, or None when never configured."""
        with durable_path_lock(self.path):
            if not self.path.exists():
                return None
            try:
                raw = self.path.read_bytes()
            except OSError as exc:
                raise OperatorSourceStoreError(
                    "operator source configuration cannot be read"
                ) from exc
            if not raw or len(raw) > _MAX_PERSISTED_BYTES:
                raise OperatorSourceStoreError(
                    "operator source configuration has invalid persisted size"
                )
            try:
                text = raw.decode("utf-8", errors="strict")
                value = strict_json_loads(text)
            except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
                raise OperatorSourceStoreError(
                    "operator source configuration is corrupt"
                ) from exc
            if type(value) is not dict or set(value) != _EXPECTED_KEYS:
                raise OperatorSourceStoreError(
                    "operator source configuration schema is invalid"
                )
            # Re-encode only to pass the decoded semantic object through the exact
            # payload validator. The persisted formatting itself is owned by
            # integrity.atomic_write_json and is not authority-bearing.
            try:
                canonical = json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8")
                return parse_operator_source_config(canonical)
            except (OperatorSourceConfigError, TypeError, ValueError, RecursionError) as exc:
                raise OperatorSourceStoreError(
                    "operator source configuration failed integrity validation"
                ) from exc

    def write(self, config: OperatorSourceConfig) -> OperatorSourceConfig:
        """Atomically publish and re-read one validated configuration."""
        if type(config) is not OperatorSourceConfig:
            raise TypeError("config must be an exact OperatorSourceConfig")
        # to_json_bytes is already strict and secret-free; decode our own trusted
        # canonical bytes into the dict shape expected by atomic_write_json.
        payload = json.loads(config.to_json_bytes().decode("utf-8"))
        with durable_path_lock(self.path):
            try:
                atomic_write_json(self.path, payload)
                persisted = self.read()
            except OperatorSourceStoreError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise OperatorSourceStoreError(
                    "operator source configuration could not be published"
                ) from exc
            if persisted != config:
                raise OperatorSourceStoreError(
                    "published operator source configuration readback mismatch"
                )
            return persisted

    def write_source_id(self, source_id: str) -> OperatorSourceConfig:
        return self.write(build_operator_source_config(source_id))

    def resolve(self, *, admin_override_source_id: str | None) -> OperatorSourceSelection:
        """Project durable state into the non-authoritative selection state machine."""
        try:
            config = self.read()
        except OperatorSourceStoreError:
            return OperatorSourceSelection(
                OperatorSourceSelectionState.INVALID,
                None,
                "persisted_config_invalid",
            )
        canonical_payload = None if config is None else config.to_json_bytes()
        return resolve_operator_source_selection(
            persisted_payload=canonical_payload,
            admin_override_source_id=admin_override_source_id,
        )
