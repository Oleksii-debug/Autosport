from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


FORBIDDEN_FUTURE_KEYS = frozenset(
    {"final_result", "result", "winner", "outcome", "settled_outcome", "future_quote"}
)


def _canonical_json_string(value: str, *, path: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{path} must contain valid UTF-8 JSON text") from exc
    return value


def _freeze_canonical_json_value(value: Any, *, path: str) -> Any:
    # This is an identity-bearing causal boundary.  Accept only the exact JSON
    # carrier types (plus our own immutable MappingProxyType snapshot) before
    # invoking any protocol/iteration/string hooks.  Otherwise a container or
    # scalar subclass could execute caller-controlled code while canonical bytes
    # are being admitted.
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is str:
        return _canonical_json_string(value, path=path)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return value
    if type(value) is dict or type(value) is MappingProxyType:
        frozen: dict[str, Any] = {}
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} JSON object keys must be exact strings")
            canonical_key = _canonical_json_string(key, path=f"{path} key")
            frozen[canonical_key] = _freeze_canonical_json_value(
                child, path=f"{path}.{canonical_key}"
            )
        return MappingProxyType(frozen)
    if type(value) is list or type(value) is tuple:
        return tuple(
            _freeze_canonical_json_value(child, path=f"{path}[{index}]")
            for index, child in enumerate(value)
        )
    raise ValueError(
        f"{path} contains unsupported JSON value type {type(value).__name__}"
    )


def freeze_canonical_json_object(value: Any, *, field_name: str) -> Mapping[str, Any]:
    """Snapshot one causal JSON object into an alias-free immutable representation."""

    if type(value) is not dict and type(value) is not MappingProxyType:
        raise ValueError(f"{field_name} must be an exact JSON object")
    frozen = _freeze_canonical_json_value(value, path=field_name)
    assert type(frozen) is MappingProxyType
    return frozen


def contains_forbidden_future_key(value: Any) -> bool:
    """Return whether a pre-outcome causal container contains future-result truth."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_FUTURE_KEYS:
                return True
            if contains_forbidden_future_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(contains_forbidden_future_key(child) for child in value)
    return False
