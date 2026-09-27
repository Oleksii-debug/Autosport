"""Seal executable helpers without retaining a callable pre-seal bypass.

The preload dispatch guard remains the canonical PaperBook persistence composition
surface.  This final seal captures only the installed wrapper's executable state and a
private globals snapshot; it deliberately does *not* retain the installed wrapper
FunctionType in the public wrapper closure.  A short-lived delegate is reconstructed
only after the captured verifier bindings/executables are validated for that call.

This closes the direct closure-extracted ``inner`` bypass while preserving the same
parser, serializer, witness protocol, store, root and economic authority.
"""

from __future__ import annotations

from types import FunctionType

from . import paper as _paper


_VERIFIER_NAMES = (
    "_require_delegate_graph_witnesses",
    "_require_class_callable_graph_witnesses",
    "_require_value_type_callable_witnesses",
)


def _verifier_witnesses(
    wrapper: FunctionType,
) -> tuple[tuple[str, FunctionType, object], ...]:
    witnesses: list[tuple[str, FunctionType, object]] = []
    for name in _VERIFIER_NAMES:
        verifier = wrapper.__globals__.get(name)
        if type(verifier) is not FunctionType:
            raise RuntimeError(f"PaperBook persistence wrapper verifier is unavailable: {name}")
        witnesses.append((name, verifier, verifier.__code__))
    return tuple(witnesses)


def _make_guarded_load(
    inner: FunctionType,
    witnesses: tuple[tuple[str, FunctionType, object], ...],
):
    exact_type = type
    function_type = FunctionType
    inner_code = inner.__code__
    inner_name = inner.__name__
    inner_defaults = inner.__defaults__
    inner_kwdefaults = None if inner.__kwdefaults__ is None else dict(inner.__kwdefaults__)
    inner_closure = inner.__closure__
    trusted_globals = dict(inner.__globals__)

    def load(cls, path):
        for name, verifier, expected_code in witnesses:
            if (
                trusted_globals.get(name) is not verifier
                or exact_type(verifier) is not function_type
                or verifier.__code__ is not expected_code
            ):
                raise ValueError("PaperBook persistence wrapper verifier executable authority changed")
        call_globals = dict(trusted_globals)
        for name, verifier, _expected_code in witnesses:
            call_globals[name] = verifier
        delegate = function_type(
            inner_code,
            call_globals,
            name=inner_name,
            argdefs=inner_defaults,
            closure=inner_closure,
        )
        if inner_kwdefaults is not None:
            delegate.__kwdefaults__ = dict(inner_kwdefaults)
        result = delegate(cls, path)
        for name, verifier, expected_code in witnesses:
            if (
                trusted_globals.get(name) is not verifier
                or exact_type(verifier) is not function_type
                or verifier.__code__ is not expected_code
            ):
                raise ValueError("PaperBook persistence wrapper verifier executable authority changed")
        return result

    load.__name__ = "load"
    load.__qualname__ = "PaperBook.load"
    return load


def _make_guarded_save(
    inner: FunctionType,
    witnesses: tuple[tuple[str, FunctionType, object], ...],
):
    exact_type = type
    function_type = FunctionType
    inner_code = inner.__code__
    inner_name = inner.__name__
    inner_defaults = inner.__defaults__
    inner_kwdefaults = None if inner.__kwdefaults__ is None else dict(inner.__kwdefaults__)
    inner_closure = inner.__closure__
    trusted_globals = dict(inner.__globals__)

    def save(self, path) -> None:
        for name, verifier, expected_code in witnesses:
            if (
                trusted_globals.get(name) is not verifier
                or exact_type(verifier) is not function_type
                or verifier.__code__ is not expected_code
            ):
                raise ValueError("PaperBook persistence wrapper verifier executable authority changed")
        call_globals = dict(trusted_globals)
        for name, verifier, _expected_code in witnesses:
            call_globals[name] = verifier
        delegate = function_type(
            inner_code,
            call_globals,
            name=inner_name,
            argdefs=inner_defaults,
            closure=inner_closure,
        )
        if inner_kwdefaults is not None:
            delegate.__kwdefaults__ = dict(inner_kwdefaults)
        delegate(self, path)
        for name, verifier, expected_code in witnesses:
            if (
                trusted_globals.get(name) is not verifier
                or exact_type(verifier) is not function_type
                or verifier.__code__ is not expected_code
            ):
                raise ValueError("PaperBook persistence wrapper verifier executable authority changed")

    save.__name__ = "save"
    save.__qualname__ = "PaperBook.save"
    return save


def _install() -> None:
    paper_book = _paper.PaperBook
    namespace = vars(paper_book)
    load_descriptor = namespace.get("load")
    save_descriptor = namespace.get("save")
    if type(load_descriptor) is not classmethod:
        raise RuntimeError("canonical PaperBook positive path load must remain a classmethod")
    if type(save_descriptor) is not FunctionType:
        raise RuntimeError("canonical PaperBook durable save must remain a Python function")

    inner_load = load_descriptor.__func__
    inner_save = save_descriptor
    if type(inner_load) is not FunctionType:
        raise RuntimeError("canonical PaperBook positive path load must remain a Python function")
    load_witnesses = _verifier_witnesses(inner_load)
    save_witnesses = _verifier_witnesses(inner_save)

    paper_book.load = classmethod(_make_guarded_load(inner_load, load_witnesses))
    paper_book.save = _make_guarded_save(inner_save, save_witnesses)


_install()
del _install
