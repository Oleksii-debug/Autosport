from __future__ import annotations

from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
import autosport.paper as paper


_HOSTILE_GETATTR_CALLS: list[str] = []


def _hostile_surface_getattr(self, name: str):
    del self
    _HOSTILE_GETATTR_CALLS.append(name)
    return object()


def _saved_book(tmp_path: Path) -> tuple[Path, paper.PaperBook]:
    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)
    return path, book


@pytest.mark.parametrize(
    ("surface_name", "member_name"),
    (("json", "loads"), ("hashlib", "sha256")),
)
def test_frozen_module_surface_backing_rejects_member_retarget(
    tmp_path: Path,
    surface_name: str,
    member_name: str,
) -> None:
    """Frozen facade diagnostics cannot retarget the admitted member authority."""

    path, book = _saved_book(tmp_path)
    surface = getattr(guard, surface_name)
    values = surface._values
    original = getattr(surface, member_name)

    with pytest.raises(TypeError):
        values[member_name] = object()

    detached = dict(values)
    detached[member_name] = object()
    assert getattr(surface, member_name) is original
    loaded = paper.PaperBook.load(path)
    assert loaded.balance == book.balance


@pytest.mark.parametrize("surface_name", ("json", "hashlib", "os", "tempfile"))
def test_frozen_module_surface_backing_binding_rejects_object_slot_bypass(
    tmp_path: Path,
    surface_name: str,
) -> None:
    """Explicit object slot operations cannot replace the facade authority payload."""

    path, book = _saved_book(tmp_path)
    surface = getattr(guard, surface_name)
    original_values = dict(surface._values)

    with pytest.raises(AttributeError):
        object.__setattr__(surface, "_values", {"hostile": object()})
    with pytest.raises(AttributeError):
        object.__delattr__(surface, "_values")

    assert dict(surface._values) == original_values
    loaded = paper.PaperBook.load(path)
    assert loaded.balance == book.balance


def test_frozen_module_surface_getattr_code_substitution_fails_before_dispatch(
    tmp_path: Path,
) -> None:
    """Facade class mutation must not become a new route to persistence authority."""

    path, _book = _saved_book(tmp_path)
    surface_type = type(guard.json)
    original_getattr = surface_type.__getattr__
    original_code = original_getattr.__code__
    _HOSTILE_GETATTR_CALLS.clear()

    original_getattr.__code__ = _hostile_surface_getattr.__code__
    try:
        with pytest.raises(
            ValueError,
            match="PaperBook persistence class executable authority changed",
        ):
            paper.PaperBook.load(path)
    finally:
        original_getattr.__code__ = original_code

    assert _HOSTILE_GETATTR_CALLS == []
