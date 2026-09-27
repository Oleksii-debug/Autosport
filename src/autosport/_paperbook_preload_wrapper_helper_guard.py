"""Seal the executable helpers used by the installed PaperBook persistence wrappers.

The preload dispatch guard freezes the canonical persistence graph, but its public
``PaperBook.load`` / ``save`` wrappers still reference verifier function objects from
their private globals mapping.  Function identity alone is insufficient because a
reachable Python function object's ``__code__`` can be replaced in place.  This final
composition layer witnesses those already-installed verifier executables and rejects
mutation before or after the existing persistence wrapper executes.

No parser, serializer, witness protocol, store, root, or economic authority is added.
"""

from __future__ import annotations

from types import FunctionType

from . import paper as _paper


_VERIFIER_NAMES = (
    "_require_delegate_graph_witnesses",
    "_require_class_callable_graph_witnesses",
    "_require_value_type_callable_witnesses",
)


def _verifier_witnesses(wrapper: FunctionType) -> tuple[tuple[FunctionType, object], ...]:
    witnesses: list[tuple[FunctionType, object]] = []
    for name in _VERIFIER_NAMES:
        verifier = wrapper.__globals__.get(name)
        if type(verifier) is not FunctionType:
            raise RuntimeError(f"PaperBook persistence wrapper verifier is unavailable: {name}")
        witnesses.append((verifier, verifier.__code__))
    return tuple(witnesses)


def _make_guarded_load(
    inner: FunctionType,
    witnesses: tuple[tuple[FunctionType, object], ...],
):
    exact_type = type
    function_type = FunctionType

    def load(cls, path):
        for verifier, expected_code in witnesses:
            if exact_type(verifier) is not function_type or verifier.__code__ is not expected_code:
                raise ValueError("PaperBook persistence wrapper verifier executable authority changed")
        result = inner(cls, path)
        for verifier, expected_code in witnesses:
            if exact_type(verifier) is not function_type or verifier.__code__ is not expected_code:
                raise ValueError("PaperBook persistence wrapper verifier executable authority changed")
        return result

    load.__name__ = "load"
    load.__qualname__ = "PaperBook.load"
    return load


def _make_guarded_save(
    inner: FunctionType,
    witnesses: tuple[tuple[FunctionType, object], ...],
):
    exact_type = type
    function_type = FunctionType

    def save(self, path) -> None:
        for verifier, expected_code in witnesses:
            if exact_type(verifier) is not function_type or verifier.__code__ is not expected_code:
                raise ValueError("PaperBook persistence wrapper verifier executable authority changed")
        inner(self, path)
        for verifier, expected_code in witnesses:
            if exact_type(verifier) is not function_type or verifier.__code__ is not expected_code:
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
    load_witnesses = _verifier_witnesses(inner_load)
    save_witnesses = _verifier_witnesses(inner_save)

    paper_book.load = classmethod(_make_guarded_load(inner_load, load_witnesses))
    paper_book.save = _make_guarded_save(inner_save, save_witnesses)


_install()
del _install
