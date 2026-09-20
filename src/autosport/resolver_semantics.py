from __future__ import annotations

import ast
import hashlib
import io
import json
from pathlib import Path
import sys
import textwrap
import tokenize
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
    """Represent executable code inside one running Python version.

    Source-location fields are deliberately absent. This payload is never persisted
    across Python versions; it is used only to verify that the currently loaded
    function still equals the function compiled from its canonical module source in
    the same interpreter.
    """

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


def _module_source(resolver: FunctionType) -> str:
    module_name = resolver.__module__
    module = sys.modules.get(module_name)
    if module is None:
        raise ResolverSemanticIdentityError("resolver module is not loaded")

    source: str | None = None
    spec = getattr(module, "__spec__", None)
    loader = getattr(spec, "loader", None)
    get_source = getattr(loader, "get_source", None)
    if callable(get_source):
        try:
            candidate = get_source(module_name)
        except (ImportError, OSError, TypeError):
            candidate = None
        if type(candidate) is str:
            source = candidate

    if source is None:
        module_file = getattr(module, "__file__", None)
        if type(module_file) is not str or not module_file:
            raise ResolverSemanticIdentityError(
                "resolver module source is unavailable"
            )
        try:
            source = Path(module_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ResolverSemanticIdentityError(
                "resolver module source is unavailable"
            ) from exc

    return source.replace("\r\n", "\n").replace("\r", "\n")


def _qualname_parts(resolver: FunctionType) -> tuple[str, ...]:
    parts = tuple(resolver.__qualname__.split("."))
    if not parts or "<locals>" in parts:
        raise ResolverSemanticIdentityError(
            "resolver must be owned by a module-level concrete class"
        )
    return parts


def _source_segment(source: str, parts: tuple[str, ...]) -> str:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        raise ResolverSemanticIdentityError(
            "resolver module source cannot be parsed"
        ) from exc

    body: list[ast.stmt] = tree.body
    node: ast.AST | None = None
    for part in parts:
        matches = [
            candidate
            for candidate in body
            if isinstance(
                candidate,
                (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            )
            and candidate.name == part
        ]
        if len(matches) != 1:
            raise ResolverSemanticIdentityError(
                "resolver source owner cannot be resolved uniquely"
            )
        node = matches[0]
        body = list(getattr(node, "body", ()))

    if not isinstance(node, ast.FunctionDef):
        raise ResolverSemanticIdentityError(
            "resolver must be a synchronous concrete method"
        )
    segment = ast.get_source_segment(source, node)
    if type(segment) is not str or not segment.strip():
        raise ResolverSemanticIdentityError(
            "resolver source segment is unavailable"
        )
    return textwrap.dedent(segment).replace("\r\n", "\n").replace("\r", "\n")


def _source_semantic_sha256(segment: str) -> str:
    canonical_tokens: list[list[str]] = []
    ignored = {tokenize.COMMENT, tokenize.NL, tokenize.ENDMARKER}
    try:
        for token in tokenize.generate_tokens(io.StringIO(segment).readline):
            if token.type in ignored:
                continue
            value = token.string
            if token.type == tokenize.INDENT:
                value = "<INDENT>"
            elif token.type == tokenize.DEDENT:
                value = "<DEDENT>"
            elif token.type == tokenize.NEWLINE:
                value = "<NEWLINE>"
            canonical_tokens.append([tokenize.tok_name[token.type], value])
    except (IndentationError, tokenize.TokenError) as exc:
        raise ResolverSemanticIdentityError(
            "resolver source cannot be canonicalized"
        ) from exc
    return hashlib.sha256(
        _canonical_json(canonical_tokens).encode("utf-8")
    ).hexdigest()


def _compiled_resolver_code(
    source: str,
    parts: tuple[str, ...],
) -> CodeType:
    try:
        current = compile(
            source,
            "<autosport-settlement-authority-source>",
            "exec",
            dont_inherit=True,
            optimize=sys.flags.optimize,
        )
    except (SyntaxError, ValueError) as exc:
        raise ResolverSemanticIdentityError(
            "resolver module source cannot be compiled"
        ) from exc

    for part in parts:
        matches = [
            value
            for value in current.co_consts
            if type(value) is CodeType and value.co_name == part
        ]
        if len(matches) != 1:
            raise ResolverSemanticIdentityError(
                "compiled resolver owner cannot be resolved uniquely"
            )
        current = matches[0]
    return current


def function_semantic_sha256(resolver: FunctionType) -> str:
    """Return a Python-version-stable authority fingerprint and verify loaded code.

    The durable digest comes from canonical source tokens, so install paths, source
    line numbers, bytecode revisions and exception-table encodings do not make an
    unchanged resolver look like a different authority after a supported Python
    runtime upgrade.

    Before returning that durable digest, the loaded function is compared with the
    code produced from the canonical module source by this same interpreter. The
    comparison therefore detects in-memory code replacement while avoiding
    persistence of version-specific bytecode.
    """

    if type(resolver) is not FunctionType:
        raise ResolverSemanticIdentityError("resolver must be an exact Python function")

    source = _module_source(resolver)
    parts = _qualname_parts(resolver)
    segment = _source_segment(source, parts)
    expected = _compiled_resolver_code(source, parts)
    if _code_payload(resolver.__code__) != _code_payload(expected):
        raise ResolverSemanticIdentityError(
            "resolver executable semantics do not match canonical module source"
        )
    return _source_semantic_sha256(segment)
