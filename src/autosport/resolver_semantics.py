from __future__ import annotations

import hashlib
import json
from types import CodeType, FunctionType


class ResolverSemanticIdentityError(ValueError):
    """Executable resolver semantics cannot be represented deterministically."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _constant_payload(value: object) -> object:
    if value is None:
        return ["none"]
    if value is Ellipsis:
        return ["ellipsis"]
    if value is NotImplemented:
        return ["not-implemented"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if type(value) is float:
        return ["float", value.hex()]
    if type(value) is complex:
        return ["complex", value.real.hex(), value.imag.hex()]
    if type(value) is str:
        return ["str", value]
    if type(value) is bytes:
        return ["bytes", value.hex()]
    if type(value) is tuple:
        return ["tuple", [_constant_payload(item) for item in value]]
    if type(value) is frozenset:
        items = [_constant_payload(item) for item in value]
        items.sort(key=_canonical_json)
        return ["frozenset", items]
    if type(value) is CodeType:
        return ["code", _code_payload(value)]
    raise ResolverSemanticIdentityError(
        f"unsupported resolver code constant type: {type(value).__module__}.{type(value).__qualname__}"
    )


def _code_payload(code: CodeType) -> dict[str, object]:
    return {
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "nlocals": code.co_nlocals,
        "stacksize": code.co_stacksize,
        "flags": code.co_flags,
        "code": code.co_code.hex(),
        "consts": [_constant_payload(value) for value in code.co_consts],
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
        "exceptiontable": getattr(code, "co_exceptiontable", b"").hex(),
    }


def function_semantic_sha256(resolver: FunctionType) -> str:
    """Hash executable function semantics while ignoring install/source locations.

    ``co_filename``, ``co_firstlineno`` and line tables are intentionally excluded so
    an identical resolver remains stable after installation relocation. Executable
    bytecode, constants (including nested code), name bindings and exception handling
    remain bound so replacing resolver behavior without changing its declared semantic
    id cannot preserve product authority.
    """

    if type(resolver) is not FunctionType:
        raise ResolverSemanticIdentityError("resolver must be an exact Python function")
    encoded = _canonical_json(_code_payload(resolver.__code__)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
