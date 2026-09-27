from __future__ import annotations

import json
from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
import autosport.paper as paper


def _authority_root(tmp_path: Path) -> str:
    return str(tmp_path.parent / f"{tmp_path.name}-paper-authority")


def test_witness_append_sequence_ignores_self_restoring_builtin_len(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Witness sequence authority must come from validated records, not mutable len()."""

    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    guarded_save = paper.PaperBook.save
    builtins_map = guarded_save.__builtins__
    original_len = builtins_map["len"]
    bypass_attempted = False

    def hostile_len(value):
        nonlocal bypass_attempted
        if (
            type(value) is list
            and value
            and type(value[-1]) is dict
            and "sequence" in value[-1]
            and "witness_sha256" in value[-1]
            and "generation" in value[-1]
            and "event" in value[-1]
        ):
            bypass_attempted = True
            builtins_map["len"] = original_len
            return original_len(value) + 7
        return original_len(value)

    builtins_map["len"] = hostile_len
    try:
        guarded_save(book, path)
    finally:
        builtins_map["len"] = original_len

    assert bypass_attempted is False
    loaded = paper.PaperBook.load(path)
    assert loaded.balance == book.balance


def test_witness_append_predecessor_ignores_self_restoring_builtin_str(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Witness predecessor authority must reuse the validated digest without str()."""

    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    witness_path = guard._witness_path(path)
    records = [
        json.loads(line)
        for line in witness_path.read_text(encoding="utf-8").splitlines()
    ]
    prior_witness_sha256 = records[-1]["witness_sha256"]
    assert type(prior_witness_sha256) is str

    guarded_save = paper.PaperBook.save
    builtins_map = guarded_save.__builtins__
    original_str = builtins_map["str"]
    bypass_attempted = False

    def hostile_str(value="", *args, **kwargs):
        nonlocal bypass_attempted
        if value == prior_witness_sha256 and not args and not kwargs:
            bypass_attempted = True
            builtins_map["str"] = original_str
            return "0" * 64
        return original_str(value, *args, **kwargs)

    builtins_map["str"] = hostile_str
    try:
        guarded_save(book, path)
    finally:
        builtins_map["str"] = original_str

    assert bypass_attempted is False
    loaded = paper.PaperBook.load(path)
    assert loaded.balance == book.balance
