from __future__ import annotations

import json
from pathlib import Path

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


def test_path_load_does_not_execute_mutated_detached_loads_cls_kwdefault(tmp_path: Path) -> None:
    """Mutable __kwdefaults__ cannot retarget an otherwise unchanged frozen function."""

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

    # PaperBook passes its canonical duplicate-key and non-finite hooks explicitly,
    # but it intentionally leaves `cls` at the json.loads default. Retargeting only
    # this detached clone's mutable kwdefault therefore reaches the positive parser
    # while function identity/code/globals/closure stay unchanged.
    kwdefaults["cls"] = HostileDecoder
    try:
        loaded = _load_or_fail_closed(path)
    finally:
        kwdefaults.clear()
        kwdefaults.update(original_kwdefaults)

    assert hostile_called is False
    if loaded is not None:
        assert loaded.balance == book.balance
