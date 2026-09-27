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
