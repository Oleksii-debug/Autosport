from __future__ import annotations

import ast
import hashlib
import io
import json
from pathlib import Path
import sys
import textwrap
import tokenize
from types import (
    BuiltinFunctionType,
    BuiltinMethodType,
    CodeType,
    FunctionType,
    ModuleType,
)


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


def _all_referenced_names(code: CodeType) -> set[str]:
    names = set(code.co_names)
    for constant in code.co_consts:
        if type(constant) is CodeType:
            names.update(_all_referenced_names(constant))
    return names


def _attribute_chain(node: ast.AST) -> tuple[str, tuple[str, ...]] | None:
    attributes: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        attributes.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name) or not attributes:
        return None
    return current.id, tuple(reversed(attributes))


def _module_attribute_dependencies(
    segment: str,
    resolver: FunctionType,
    *,
    visiting: set[str],
) -> dict[str, object]:
    try:
        tree = ast.parse(segment)
    except (SyntaxError, ValueError) as exc:
        raise ResolverSemanticIdentityError(
            "resolver source cannot be inspected for module dependencies"
        ) from exc

    dependencies: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        chain = _attribute_chain(node)
        if chain is None:
            continue
        root_name, attributes = chain
        root = resolver.__globals__.get(root_name)
        if type(root) is not ModuleType:
            continue

        value: object = root
        for attribute in attributes:
            try:
                value = getattr(value, attribute)
            except AttributeError as exc:
                raise ResolverSemanticIdentityError(
                    "referenced module attribute cannot be resolved"
                ) from exc

        # Every statically resolved qualified value can influence resolver
        # behavior. Canonical immutable constants are sealed directly; mutable or
        # opaque values fail closed through _dependency_payload rather than
        # disappearing behind the root module identity.
        key = f"module:{root_name}.{'.'.join(attributes)}"
        try:
            dependencies[key] = _dependency_payload(value, visiting=visiting)
        except ResolverSemanticIdentityError as exc:
            raise ResolverSemanticIdentityError(
                "referenced module qualified data cannot be represented safely"
            ) from exc
    return dependencies


def _resolve_global_type_attribute(
    owner: type,
    attribute: str,
) -> tuple[object, list[str], type | None]:
    """Resolve one class attribute without invoking opaque descriptors.

    dispatch_owner preserves the concrete class through which an inherited
    classmethod/ordinary method is reached. The declaring owner alone is not
    enough: classmethod cls dispatch can depend on helpers overridden on a
    subclass even when the inherited method's own code is unchanged.
    """

    resolved_owner: type | None = None
    raw: object = None
    for candidate in owner.__mro__:
        namespace = vars(candidate)
        if attribute in namespace:
            resolved_owner = candidate
            raw = namespace[attribute]
            break
    if resolved_owner is None:
        raise ResolverSemanticIdentityError(
            "referenced global type attribute cannot be resolved"
        )

    descriptor_kind: str
    value: object
    dispatch_owner: type | None = None
    if type(raw) is staticmethod:
        descriptor_kind = "staticmethod"
        value = raw.__func__
    elif type(raw) is classmethod:
        descriptor_kind = "classmethod"
        value = raw.__func__
        dispatch_owner = owner
    elif type(raw) is FunctionType:
        descriptor_kind = "function"
        value = raw
        dispatch_owner = owner
    elif type(raw) is type:
        descriptor_kind = "type"
        value = raw
    else:
        descriptor_kind = f"{type(raw).__module__}.{type(raw).__qualname__}"
        if getattr(type(raw), "__get__", None) is not None:
            raise ResolverSemanticIdentityError(
                "referenced global type descriptor cannot be resolved safely"
            )
        value = raw

    return value, [
        f"{resolved_owner.__module__}.{resolved_owner.__qualname__}",
        descriptor_kind,
        attribute,
    ], dispatch_owner


def _global_type_attribute_dependencies(
    segment: str,
    resolver: FunctionType,
    *,
    visiting: set[str],
) -> dict[str, object]:
    """Seal executable attribute chains rooted at directly referenced global types."""

    try:
        tree = ast.parse(segment)
    except (SyntaxError, ValueError) as exc:
        raise ResolverSemanticIdentityError(
            "resolver source cannot be inspected for global type dependencies"
        ) from exc

    dependencies: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        chain = _attribute_chain(node)
        if chain is None:
            continue
        root_name, attributes = chain
        root = resolver.__globals__.get(root_name)
        if type(root) is not type:
            continue

        value: object = root
        path: list[list[str]] = []
        complete = True
        dispatch_owner: type | None = None
        for attribute in attributes:
            if type(value) is not type:
                complete = False
                break
            value, step, dispatch_owner = _resolve_global_type_attribute(
                value,
                attribute,
            )
            path.append(step)
        if not complete:
            continue

        if type(value) is FunctionType:
            dependency_payload: object = [
                "function",
                _function_semantic_payload(
                    value,
                    visiting=visiting,
                    runtime_owner=dispatch_owner,
                ),
            ]
        else:
            try:
                dependency_payload = _dependency_payload(value, visiting=visiting)
            except ResolverSemanticIdentityError as exc:
                raise ResolverSemanticIdentityError(
                    "referenced global type qualified data cannot be represented safely"
                ) from exc

        key = f"global-type:{root_name}.{'.'.join(attributes)}"
        dependencies[key] = [
            "global-type-attribute-chain",
            f"{root.__module__}.{root.__qualname__}",
            path,
            dependency_payload,
        ]
    return dependencies


def _default_constructor_dependency(owner: type) -> object:
    """Represent a zero-argument constructor without executing it.

    Instance-dispatch dependencies are admitted only when construction uses the
    exact inherited object defaults. Any custom __new__/__init__ remains
    unsupported and fails closed rather than being invoked during fingerprinting.
    """

    resolved_new_owner: type | None = None
    resolved_init_owner: type | None = None
    for candidate in owner.__mro__:
        namespace = vars(candidate)
        if resolved_new_owner is None and "__new__" in namespace:
            resolved_new_owner = candidate
        if resolved_init_owner is None and "__init__" in namespace:
            resolved_init_owner = candidate
        if resolved_new_owner is not None and resolved_init_owner is not None:
            break

    if resolved_new_owner is not object or resolved_init_owner is not object:
        raise ResolverSemanticIdentityError(
            "referenced global type constructor cannot be represented safely"
        )

    return [
        "default-object-constructor",
        f"{owner.__module__}.{owner.__qualname__}",
    ]


def _default_instance_lookup_dependency(owner: type) -> object:
    """Require ordinary instance lookup semantics without invoking callbacks."""

    resolved_getattribute_owner: type | None = None
    raw_getattribute: object = None
    for candidate in owner.__mro__:
        namespace = vars(candidate)
        if "__getattribute__" in namespace:
            resolved_getattribute_owner = candidate
            raw_getattribute = namespace["__getattribute__"]
            break

    if (
        resolved_getattribute_owner is not object
        or raw_getattribute is not object.__getattribute__
    ):
        raise ResolverSemanticIdentityError(
            "referenced global type instance lookup cannot be represented safely"
        )

    for candidate in owner.__mro__:
        if "__getattr__" in vars(candidate):
            raise ResolverSemanticIdentityError(
                "referenced global type fallback lookup cannot be represented safely"
            )

    return [
        "default-object-attribute-lookup",
        f"{owner.__module__}.{owner.__qualname__}",
    ]


def _global_type_instance_dependencies(
    segment: str,
    resolver: FunctionType,
    *,
    visiting: set[str],
) -> dict[str, object]:
    """Seal safe zero-argument Class().method dispatch without construction."""

    try:
        tree = ast.parse(segment)
    except (SyntaxError, ValueError) as exc:
        raise ResolverSemanticIdentityError(
            "resolver source cannot be inspected for global type instance dependencies"
        ) from exc

    dependencies: dict[str, object] = {}
    executable_types = (
        FunctionType,
        BuiltinFunctionType,
        BuiltinMethodType,
        ModuleType,
        type,
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        call = node.value
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue

        root_name = call.func.id
        root = resolver.__globals__.get(root_name)
        if type(root) is not type:
            continue
        if call.args or call.keywords:
            raise ResolverSemanticIdentityError(
                "referenced global type constructor arguments cannot be represented safely"
            )

        lookup_payload = _default_instance_lookup_dependency(root)
        constructor_payload = _default_constructor_dependency(root)
        value, step, dispatch_owner = _resolve_global_type_attribute(
            root,
            node.attr,
        )
        if type(value) not in executable_types:
            continue

        if type(value) is FunctionType:
            dependency_payload: object = [
                "function",
                _function_semantic_payload(
                    value,
                    visiting=visiting,
                    runtime_owner=dispatch_owner,
                ),
            ]
        else:
            dependency_payload = _dependency_payload(value, visiting=visiting)

        key = f"global-type-instance:{root_name}().{node.attr}"
        dependencies[key] = [
            "global-type-instance-dispatch",
            f"{root.__module__}.{root.__qualname__}",
            constructor_payload,
            lookup_payload,
            step,
            dependency_payload,
        ]
    return dependencies


def _owner_class(resolver: FunctionType) -> type | None:
    parts = _qualname_parts(resolver)
    if len(parts) < 2:
        return None
    module = sys.modules.get(resolver.__module__)
    if module is None:
        raise ResolverSemanticIdentityError("resolver module is not loaded")
    owner: object = module
    for part in parts[:-1]:
        try:
            owner = getattr(owner, part)
        except AttributeError as exc:
            raise ResolverSemanticIdentityError(
                "resolver owner class cannot be resolved"
            ) from exc
    if type(owner) is not type:
        raise ResolverSemanticIdentityError(
            "resolver owner must be an exact concrete class"
        )
    return owner


def _dependency_payload(
    value: object,
    *,
    visiting: set[str],
) -> object:
    if type(value) is FunctionType:
        return ["function", _function_semantic_payload(value, visiting=visiting)]
    if type(value) in (BuiltinFunctionType, BuiltinMethodType):
        module_name = getattr(value, "__module__", None)
        qualname = getattr(value, "__qualname__", getattr(value, "__name__", None))
        if type(module_name) is not str or type(qualname) is not str:
            raise ResolverSemanticIdentityError(
                "referenced builtin callable lacks stable identity"
            )
        return ["builtin", module_name, qualname]
    if type(value) is ModuleType:
        module_name = getattr(value, "__name__", None)
        if type(module_name) is not str or not module_name:
            raise ResolverSemanticIdentityError(
                "referenced module lacks stable identity"
            )
        return ["module", module_name]
    if type(value) is type:
        return ["type", value.__module__, value.__qualname__]
    try:
        return ["constant", _constant_payload(value)]
    except ResolverSemanticIdentityError as exc:
        raise ResolverSemanticIdentityError(
            "resolver references unsupported mutable or opaque global authority"
        ) from exc


def _class_dependency(
    owner: type | None,
    name: str,
    *,
    visiting: set[str],
) -> object | None:
    if owner is None:
        return None

    resolved_owner: type | None = None
    raw: object = None
    for candidate in owner.__mro__:
        namespace = vars(candidate)
        if name in namespace:
            resolved_owner = candidate
            raw = namespace[name]
            break
    if resolved_owner is None:
        return None

    if type(raw) is staticmethod or type(raw) is classmethod:
        raw = raw.__func__
    elif type(raw) is property:
        if raw.fget is None:
            raise ResolverSemanticIdentityError(
                "resolver references property without a getter"
            )
        raw = raw.fget
    return [
        "class-attribute",
        f"{resolved_owner.__module__}.{resolved_owner.__qualname__}",
        _dependency_payload(raw, visiting=visiting),
    ]


def _function_semantic_payload(
    resolver: FunctionType,
    *,
    visiting: set[str],
    runtime_owner: type | None = None,
) -> dict[str, object]:
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

    source_sha256 = _source_semantic_sha256(segment)
    semantic_key = f"{resolver.__module__}.{resolver.__qualname__}"
    if semantic_key in visiting:
        return {
            "source_sha256": source_sha256,
            "cycle": True,
        }

    next_visiting = set(visiting)
    next_visiting.add(semantic_key)
    declared_owner = _owner_class(resolver)
    if runtime_owner is not None:
        if type(runtime_owner) is not type:
            raise ResolverSemanticIdentityError(
                "runtime resolver owner must be an exact class"
            )
        if declared_owner is not None and declared_owner not in runtime_owner.__mro__:
            raise ResolverSemanticIdentityError(
                "runtime resolver owner does not inherit the declaring owner"
            )
        owner = runtime_owner
    else:
        owner = declared_owner
    dependencies: dict[str, object] = {}
    for name in sorted(_all_referenced_names(resolver.__code__)):
        class_dependency = _class_dependency(
            owner,
            name,
            visiting=next_visiting,
        )
        if class_dependency is not None:
            dependencies[f"class:{name}"] = class_dependency
            continue
        if name in resolver.__globals__:
            dependencies[f"global:{name}"] = _dependency_payload(
                resolver.__globals__[name],
                visiting=next_visiting,
            )

    dependencies.update(
        _module_attribute_dependencies(
            segment,
            resolver,
            visiting=next_visiting,
        )
    )
    dependencies.update(
        _global_type_attribute_dependencies(
            segment,
            resolver,
            visiting=next_visiting,
        )
    )
    dependencies.update(
        _global_type_instance_dependencies(
            segment,
            resolver,
            visiting=next_visiting,
        )
    )

    return {
        "source_sha256": source_sha256,
        "dependencies": dependencies,
    }


def function_semantic_sha256(
    resolver: FunctionType,
    *,
    runtime_owner: type | None = None,
) -> str:
    """Fingerprint resolver source plus referenced authority-bearing dependencies.

    The persisted digest is derived from canonical source tokens and a recursively
    sealed graph of referenced Python helpers/constants, not from version-specific
    bytecode. Each Python function in that graph is also checked against the code
    compiled from its canonical module source by the current interpreter, so runtime
    code mutation is rejected before an authority-bearing read.
    """

    payload = _function_semantic_payload(
        resolver,
        visiting=set(),
        runtime_owner=runtime_owner,
    )
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
