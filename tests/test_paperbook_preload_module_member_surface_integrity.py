from __future__ import annotations

from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
import autosport.paper as paper


def _surface_values(surface: object) -> dict[str, object]:
    values = getattr(surface, "_values")
    assert type(values) is dict
    return values


def test_frozen_json_surface_does_not_expose_mutable_loads_dispatch(tmp_path: Path) -> None:
    """The facade itself must not provide a dict escape hatch back into witness parsing."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    values = _surface_values(guard.json)
    original_loads = values["loads"]
    bypass_attempted = False

    def hostile_loads(*args, **kwargs):
        nonlocal bypass_attempted
        bypass_attempted = True
        values["loads"] = original_loads
        return original_loads(*args, **kwargs)

    values["loads"] = hostile_loads
    try:
        loaded = paper.PaperBook.load(path)
    finally:
        values["loads"] = original_loads

    assert bypass_attempted is False
    assert loaded.balance == book.balance


def test_frozen_hash_surface_does_not_expose_mutable_digest_dispatch(tmp_path: Path) -> None:
    """The digest facade must not retain an externally mutable member table."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    values = _surface_values(guard.hashlib)
    original_sha256 = values["sha256"]
    bypass_attempted = False

    def hostile_sha256(*args, **kwargs):
        nonlocal bypass_attempted
        bypass_attempted = True
        values["sha256"] = original_sha256
        return original_sha256(*args, **kwargs)

    values["sha256"] = hostile_sha256
    try:
        loaded = paper.PaperBook.load(path)
    finally:
        values["sha256"] = original_sha256

    assert bypass_attempted is False
    assert loaded.balance == book.balance
