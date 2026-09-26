"""Seal durable continuous-observation restart parsing to canonical JSON integrity.

This guard does not implement another parser. It witnesses the existing
``autosport.json_integrity.strict_json_loads`` dependency graph and delegates to the
existing continuous-observation status reader only while that graph remains canonical.
"""

from __future__ import annotations

from . import continuous_observation as _observation
from . import json_integrity as _json_integrity


def _install() -> None:
    canonical_reader = _observation._read_previous_status
    canonical_reader_code = canonical_reader.__code__
    canonical_loader = _json_integrity.strict_json_loads
    canonical_loader_code = canonical_loader.__code__
    canonical_json = _json_integrity.json
    canonical_json_loads = canonical_json.loads
    canonical_math = _json_integrity.math
    canonical_isfinite = canonical_math.isfinite

    helper_names = (
        "_unique_json_object",
        "_reject_nonstandard_json_constant",
        "_parse_bounded_json_integer",
        "_validate_strict_json_value",
    )
    helper_witnesses = tuple(
        (name, getattr(_json_integrity, name), getattr(_json_integrity, name).__code__)
        for name in helper_names
    )
    canonical_integer_limit = _json_integrity._JSON_INTEGER_MAX_DIGITS

    def parser_dispatch_is_canonical() -> bool:
        if (
            _observation.strict_json_loads is not canonical_loader
            or _observation._read_previous_status is not guarded_reader
            or canonical_reader.__code__ is not canonical_reader_code
            or _json_integrity.strict_json_loads is not canonical_loader
            or canonical_loader.__code__ is not canonical_loader_code
            or _json_integrity.json is not canonical_json
            or canonical_json.loads is not canonical_json_loads
            or _json_integrity.math is not canonical_math
            or canonical_math.isfinite is not canonical_isfinite
            or _json_integrity._JSON_INTEGER_MAX_DIGITS != canonical_integer_limit
        ):
            return False
        for name, expected, expected_code in helper_witnesses:
            current = getattr(_json_integrity, name, None)
            if current is not expected or getattr(expected, "__code__", None) is not expected_code:
                return False
        return True

    def guarded_reader(path):
        if not parser_dispatch_is_canonical():
            raise ValueError("continuous observation status JSON parser authority changed")
        result = canonical_reader(path)
        if not parser_dispatch_is_canonical():
            raise ValueError("continuous observation status JSON parser authority changed")
        return result

    _observation._read_previous_status = guarded_reader


_install()
del _install

__all__: list[str] = []
