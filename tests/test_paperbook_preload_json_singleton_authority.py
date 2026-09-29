from __future__ import annotations

import json
from pathlib import Path

import pytest

import autosport.paper as paper


_MISSING = object()


def _load_or_fail_closed(path: Path):
    try:
        return paper.PaperBook.load(path)
    except ValueError as exc:
        assert any(token in str(exc).lower() for token in ("authority", "executable", "transitive"))
        return None


def test_path_load_does_not_execute_mutated_shared_default_decoder_instance(tmp_path: Path) -> None:
    """Detached json.loads must not inherit mutable singleton decoder dispatch."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    frozen_loads = paper.json.loads
    decoder = frozen_loads.__globals__.get("_default_decoder")
    assert decoder is json._default_decoder

    original_instance_decode = vars(decoder).get("decode", _MISSING)
    original_bound_decode = decoder.decode
    hostile_called = False

    def hostile_decode(payload: str):
        nonlocal hostile_called
        hostile_called = True
        return original_bound_decode(payload)

    decoder.decode = hostile_decode
    try:
        loaded = _load_or_fail_closed(path)
    finally:
        if original_instance_decode is _MISSING:
            del decoder.decode
        else:
            decoder.decode = original_instance_decode

    assert hostile_called is False
    if loaded is not None:
        assert loaded.balance == book.balance


def test_path_load_does_not_execute_mutated_detached_loads_kwdefault(tmp_path: Path) -> None:
    """Mutable __kwdefaults__ cannot retarget an otherwise unchanged frozen function."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    frozen_loads = paper.json.loads
    kwdefaults = frozen_loads.__kwdefaults__
    assert type(kwdefaults) is dict
    original_kwdefaults = dict(kwdefaults)
    hostile_called = False

    def hostile_object_hook(value: dict[str, object]) -> dict[str, object]:
        nonlocal hostile_called
        hostile_called = True
        return value

    kwdefaults["object_hook"] = hostile_object_hook
    try:
        loaded = _load_or_fail_closed(path)
    finally:
        kwdefaults.clear()
        kwdefaults.update(original_kwdefaults)

    assert hostile_called is False
    if loaded is not None:
        assert loaded.balance == book.balance
