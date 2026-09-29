from __future__ import annotations

from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
import autosport.paper as paper


_HOSTILE_GETATTRIBUTE_CALLS: list[str] = []


def _hostile_getattribute(self, name: str):
    _HOSTILE_GETATTRIBUTE_CALLS.append(name)
    if name == "loads":
        return lambda _raw: {"balance": "999999"}
    return object.__getattribute__(self, name)


def test_frozen_surface_getattribute_cannot_be_added_after_composition(
    tmp_path: Path,
) -> None:
    """A new lookup root must not bypass the sealed __getattr__ member authority."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    surface_type = type(guard.json)
    namespace = vars(surface_type)
    original = namespace.get("__getattribute__")
    _HOSTILE_GETATTRIBUTE_CALLS.clear()

    assert original is object.__getattribute__
    with pytest.raises(TypeError, match="frozen-surface root is sealed"):
        setattr(surface_type, "__getattribute__", _hostile_getattribute)
    with pytest.raises(TypeError, match="frozen-surface root is sealed"):
        type.__setattr__(surface_type, "__getattribute__", _hostile_getattribute)
    with pytest.raises(TypeError, match="frozen-surface root is sealed"):
        delattr(surface_type, "__getattribute__")
    with pytest.raises(TypeError, match="frozen-surface root is sealed"):
        type.__delattr__(surface_type, "__getattribute__")

    assert vars(surface_type)["__getattribute__"] is original
    loaded = paper.PaperBook.load(path)
    assert loaded.balance == book.balance
    assert _HOSTILE_GETATTRIBUTE_CALLS == []
