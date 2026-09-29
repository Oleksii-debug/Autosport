from __future__ import annotations

import json
from pathlib import Path

import autosport._paperbook_preload_authority_guard as guard
import autosport.paper as paper


def _load_or_fail_closed(path: Path):
    try:
        return paper.PaperBook.load(path)
    except (TypeError, ValueError) as exc:
        assert any(
            token in str(exc).lower()
            for token in ("authority", "executable", "transitive", "defaults")
        )
        return None


def _save_or_fail_closed(book: paper.PaperBook, path: Path) -> bool:
    try:
        book.save(path)
    except (TypeError, ValueError) as exc:
        assert any(
            token in str(exc).lower()
            for token in ("authority", "executable", "transitive", "defaults")
        )
        return False
    return True


def test_path_load_does_not_execute_mutated_detached_loads_cls_kwdefault(tmp_path: Path) -> None:
    """Mutable parser __kwdefaults__ cannot retarget an unchanged frozen function."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    frozen_loads = paper.json.loads
    kwdefaults = frozen_loads.__kwdefaults__
    assert type(kwdefaults) is dict
    original_kwdefaults = dict(kwdefaults)
    hostile_called = False

    class HostileDecoder(json.JSONDecoder):
        def __init__(self, *args, **kwargs) -> None:
            nonlocal hostile_called
            hostile_called = True
            super().__init__(*args, **kwargs)

    # PaperBook passes duplicate-key/non-finite hooks explicitly but leaves `cls`
    # at the json.loads default. Mutating only this detached clone's kwdefault used
    # to reach the parser while function identity/code/globals/closure stayed exact.
    kwdefaults["cls"] = HostileDecoder
    try:
        loaded = _load_or_fail_closed(path)
    finally:
        kwdefaults.clear()
        kwdefaults.update(original_kwdefaults)

    assert hostile_called is False
    if loaded is not None:
        assert loaded.balance == book.balance


def test_witness_load_does_not_execute_mutated_detached_loads_cls_kwdefault(tmp_path: Path) -> None:
    """Independent witness parsing has the same exact frozen-default requirement."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    frozen_loads = guard.json.loads
    kwdefaults = frozen_loads.__kwdefaults__
    assert type(kwdefaults) is dict
    original_kwdefaults = dict(kwdefaults)
    hostile_called = False

    class HostileDecoder(json.JSONDecoder):
        def __init__(self, *args, **kwargs) -> None:
            nonlocal hostile_called
            hostile_called = True
            super().__init__(*args, **kwargs)

    kwdefaults["cls"] = HostileDecoder
    try:
        loaded = _load_or_fail_closed(path)
    finally:
        kwdefaults.clear()
        kwdefaults.update(original_kwdefaults)

    assert hostile_called is False
    if loaded is not None:
        assert loaded.balance == book.balance


def test_save_does_not_execute_mutated_detached_dump_cls_kwdefault(tmp_path: Path) -> None:
    """Serializer keyword defaults are authority, not caller-mutable configuration."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")

    frozen_dump = paper.json.dump
    kwdefaults = frozen_dump.__kwdefaults__
    assert type(kwdefaults) is dict
    original_kwdefaults = dict(kwdefaults)
    hostile_called = False

    class HostileEncoder(json.JSONEncoder):
        def __init__(self, *args, **kwargs) -> None:
            nonlocal hostile_called
            hostile_called = True
            super().__init__(*args, **kwargs)

    kwdefaults["cls"] = HostileEncoder
    try:
        saved = _save_or_fail_closed(book, path)
    finally:
        kwdefaults.clear()
        kwdefaults.update(original_kwdefaults)

    assert hostile_called is False
    if saved:
        assert paper.PaperBook.load(path).balance == book.balance
    else:
        assert not path.exists()
