from __future__ import annotations

from pathlib import Path
from types import FunctionType

import pytest

import autosport.paper as paper


def _clone_with_builtins(function: FunctionType, builtins_map: dict[str, object]) -> FunctionType:
    globals_copy = dict(function.__globals__)
    globals_copy["__builtins__"] = builtins_map
    clone = FunctionType(
        function.__code__,
        globals_copy,
        name=function.__name__,
        argdefs=function.__defaults__,
        closure=function.__closure__,
    )
    if function.__kwdefaults__ is not None:
        clone.__kwdefaults__ = dict(function.__kwdefaults__)
    return clone


class _HostilePaperBook(paper.PaperBook):
    def _validate_loaded_state(self, book: paper.PaperBook) -> None:
        del book
        raise AssertionError("subclass serializer dispatch reached")


def test_durable_save_exact_type_fence_ignores_self_restoring_builtin_substitution(
    tmp_path: Path,
) -> None:
    """Mutable function builtins cannot authorize subclass serializer dispatch."""

    book = _HostilePaperBook("100")
    guarded_save = paper.PaperBook.save
    builtins_map = dict(guarded_save.__builtins__)
    original_type = builtins_map["type"]
    bypass_attempted = False

    def hostile_type(value):
        nonlocal bypass_attempted
        if value is book:
            bypass_attempted = True
            builtins_map["type"] = original_type
            return paper.PaperBook
        return original_type(value)

    builtins_map["type"] = hostile_type
    isolated_save = _clone_with_builtins(guarded_save, builtins_map)
    with pytest.raises(TypeError, match="canonical PaperBook class"):
        isolated_save(book, tmp_path / "paper-book.json")

    assert bypass_attempted is False
    assert not (tmp_path / "paper-book.json").exists()


def test_next_generation_ignores_self_restoring_builtin_int_substitution(
    tmp_path: Path,
) -> None:
    """Durable generation arithmetic must not dispatch through mutable builtin int."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    guarded_save = paper.PaperBook.save
    builtins_map = dict(guarded_save.__builtins__)
    original_int = builtins_map["int"]
    bypass_attempted = False

    def hostile_int(value=0, *args, **kwargs):
        nonlocal bypass_attempted
        if original_int is type(value) and value == 1 and not args and not kwargs:
            bypass_attempted = True
            builtins_map["int"] = original_int
            return 999
        return original_int(value, *args, **kwargs)

    builtins_map["int"] = hostile_int
    isolated_save = _clone_with_builtins(guarded_save, builtins_map)
    isolated_save(book, path)

    assert bypass_attempted is False
    loaded = paper.PaperBook.load(path)
    assert loaded.balance == book.balance
