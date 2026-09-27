from __future__ import annotations

import hashlib
import json
from pathlib import Path

import autosport.paper as paper


def test_path_load_ignores_self_restoring_json_loads_substitution(tmp_path: Path) -> None:
    """Positive witness parsing must not dispatch through mutable stdlib json.loads."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    original_loads = json.loads
    bypass_attempted = False

    def hostile_loads(*args, **kwargs):
        nonlocal bypass_attempted
        bypass_attempted = True
        json.loads = original_loads
        return original_loads(*args, **kwargs)

    json.loads = hostile_loads
    try:
        loaded = paper.PaperBook.load(path)
    finally:
        json.loads = original_loads

    assert bypass_attempted is False
    assert loaded.balance == book.balance


def test_path_load_ignores_self_restoring_hashlib_sha256_substitution(tmp_path: Path) -> None:
    """Snapshot/witness digest authority must not dispatch through mutable hashlib.sha256."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    original_sha256 = hashlib.sha256
    bypass_attempted = False

    def hostile_sha256(*args, **kwargs):
        nonlocal bypass_attempted
        bypass_attempted = True
        hashlib.sha256 = original_sha256
        return original_sha256(*args, **kwargs)

    hashlib.sha256 = hostile_sha256
    try:
        loaded = paper.PaperBook.load(path)
    finally:
        hashlib.sha256 = original_sha256

    assert bypass_attempted is False
    assert loaded.balance == book.balance
