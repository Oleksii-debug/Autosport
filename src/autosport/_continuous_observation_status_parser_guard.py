"""Seal durable continuous-observation restart parsing to canonical JSON integrity.

This guard does not implement another parser. It witnesses the existing
``autosport.json_integrity.strict_json_loads`` dependency graph and delegates to the
existing continuous-observation status reader only while that graph remains canonical.
"""

from __future__ import annotations

import builtins as _builtins

from . import continuous_observation as _observation
from . import json_integrity as _json_integrity


def _install() -> None:
    canonical_reader = _observation._read_previous_status
    canonical_reader_code = canonical_reader.__code__
    canonical_loader = _json_integrity.strict_json_loads
    canonical_loader_code = canonical_loader.__code__
    canonical_json = _json_integrity.json
    canonical_json_loads = canonical_json.loads
    canonical_json_loads_code = getattr(canonical_json_loads, "__code__", None)
    canonical_json_decoder = canonical_json.JSONDecoder
    decoder_method_witnesses = tuple(
        (name, getattr(canonical_json_decoder, name), getattr(canonical_json_decoder, name).__code__)
        for name in ("__init__", "decode", "raw_decode")
    )
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

    # The helper code above still resolves ordinary builtin names through the module
    # globals / module ``__builtins__`` mapping.  Pin that executable dependency too:
    # e.g. rebinding ``ord`` can otherwise make raw JSON digit text decode to another
    # integer while every helper function object and ``__code__`` remains unchanged.
    builtin_names = (
        "bool",
        "dict",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "ord",
        "str",
        "type",
    )
    canonical_builtin_bindings = tuple(
        (name, getattr(_builtins, name)) for name in builtin_names
    )
    canonical_module_builtins = _json_integrity.__dict__.get("__builtins__")
    if canonical_module_builtins is None:
        raise RuntimeError("json_integrity builtin dispatch is unavailable")
    module_builtins_is_dict = type(canonical_module_builtins) is dict

    def builtin_dispatch_is_canonical() -> bool:
        if _json_integrity.__dict__.get("__builtins__") is not canonical_module_builtins:
            return False
        for name, expected in canonical_builtin_bindings:
            # An injected module global shadows builtin fallback even when the module's
            # ``__builtins__`` object itself did not move.
            if _json_integrity.__dict__.get(name, expected) is not expected:
                return False
            if module_builtins_is_dict:
                if canonical_module_builtins.get(name) is not expected:
                    return False
            elif getattr(canonical_module_builtins, name, None) is not expected:
                return False
        return True

    def parser_dispatch_is_canonical() -> bool:
        if (
            _observation.strict_json_loads is not canonical_loader
            or _observation._read_previous_status is not guarded_reader
            or canonical_reader.__code__ is not canonical_reader_code
            or _json_integrity.strict_json_loads is not canonical_loader
            or canonical_loader.__code__ is not canonical_loader_code
            or _json_integrity.json is not canonical_json
            or canonical_json.loads is not canonical_json_loads
            or getattr(canonical_json_loads, "__code__", None) is not canonical_json_loads_code
            or canonical_json.JSONDecoder is not canonical_json_decoder
            or _json_integrity.math is not canonical_math
            or canonical_math.isfinite is not canonical_isfinite
            or _json_integrity._JSON_INTEGER_MAX_DIGITS != canonical_integer_limit
            or not builtin_dispatch_is_canonical()
        ):
            return False
        for name, expected, expected_code in decoder_method_witnesses:
            current = getattr(canonical_json_decoder, name, None)
            if current is not expected or getattr(expected, "__code__", None) is not expected_code:
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
