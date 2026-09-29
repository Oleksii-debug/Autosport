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


def test_frozen_surface_getattribute_retarget_fails_before_member_dispatch(
    tmp_path: Path,
) -> None:
    """Primary lookup replacement must not bypass frozen persistence authority."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    surface_type = type(guard.json)
    original = vars(surface_type).get("__getattribute__")
    assert callable(original)
    _HOSTILE_GETATTRIBUTE_CALLS.clear()

    type.__setattr__(surface_type, "__getattribute__", _hostile_getattribute)
    try:
        with pytest.raises(
            ValueError,
            match="PaperBook persistence class executable authority changed",
        ):
            paper.PaperBook.load(path)
    finally:
        type.__setattr__(surface_type, "__getattribute__", original)

    loaded = paper.PaperBook.load(path)
    assert loaded.balance == book.balance
    assert _HOSTILE_GETATTRIBUTE_CALLS == []
