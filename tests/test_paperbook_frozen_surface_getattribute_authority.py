from __future__ import annotations

import marshal
from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
import autosport._paperbook_preload_module_member_freeze as freeze
import autosport.paper as paper


_HOSTILE_GETATTRIBUTE_CALLS: list[str] = []
_HOSTILE_OBJECT_CALLS: list[str] = []


def _hostile_getattribute(self, name: str):
    _HOSTILE_GETATTRIBUTE_CALLS.append(name)
    if name == "loads":
        return lambda _raw: {"balance": "999999"}
    return object.__getattribute__(self, name)


class _HostileObject:
    @staticmethod
    def __getattribute__(self, name: str):
        _HOSTILE_OBJECT_CALLS.append(name)
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
            match="PaperBook persistence class (?:executable|dispatch) authority changed",
        ):
            paper.PaperBook.load(path)
    finally:
        type.__setattr__(surface_type, "__getattribute__", original)

    loaded = paper.PaperBook.load(path)
    assert loaded.balance == book.balance
    assert _HOSTILE_GETATTRIBUTE_CALLS == []


def test_frozen_surface_getattribute_ignores_late_object_global_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primary lookup is global-independent and remains marshalable for packaging."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)
    surface_type = type(guard.json)
    lookup = vars(surface_type)["__getattribute__"]

    assert "object" not in lookup.__code__.co_names
    assert "tuple" not in lookup.__code__.co_names
    assert all(not callable(item) for item in lookup.__code__.co_consts)
    assert marshal.loads(marshal.dumps(lookup.__code__)).co_code == lookup.__code__.co_code

    _HOSTILE_OBJECT_CALLS.clear()
    monkeypatch.setattr(freeze, "object", _HostileObject, raising=False)
    loaded = paper.PaperBook.load(path)

    assert loaded.balance == book.balance
    assert _HOSTILE_OBJECT_CALLS == []
