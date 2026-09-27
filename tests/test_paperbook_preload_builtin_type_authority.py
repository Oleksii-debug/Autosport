from __future__ import annotations

from pathlib import Path

import pytest

import autosport.paper as paper


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
    builtins_map = guarded_save.__builtins__
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
    try:
        with pytest.raises(TypeError, match="canonical PaperBook class"):
            guarded_save(book, tmp_path / "paper-book.json")
    finally:
        builtins_map["type"] = original_type

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
    builtins_map = guarded_save.__builtins__
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
    try:
        guarded_save(book, path)
    finally:
        builtins_map["int"] = original_int

    assert bypass_attempted is False
    loaded = paper.PaperBook.load(path)
    assert loaded.balance == book.balance
