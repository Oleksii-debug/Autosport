from __future__ import annotations

import json
from pathlib import Path

import autosport.paper as paper


def _load_or_fail_closed(path: Path):
    try:
        return paper.PaperBook.load(path)
    except (TypeError, ValueError):
        return None


def _save_or_fail_closed(book: paper.PaperBook, path: Path) -> bool:
    try:
        book.save(path)
    except (TypeError, ValueError):
        return False
    return True


def test_save_does_not_execute_self_restoring_public_json_encoder_method(
    tmp_path: Path,
) -> None:
    """A shared JSONEncoder method cannot retarget the frozen PaperBook serializer."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    original_iterencode = json.JSONEncoder.iterencode
    hostile_called = False

    def hostile_iterencode(self, value, _one_shot=False):
        nonlocal hostile_called
        hostile_called = True
        # Restore before delegating so a simple post-call descriptor witness cannot
        # detect that hostile executable dispatch already occurred in flight.
        json.JSONEncoder.iterencode = original_iterencode
        return original_iterencode(self, value, _one_shot)

    json.JSONEncoder.iterencode = hostile_iterencode
    try:
        saved = _save_or_fail_closed(book, path)
    finally:
        json.JSONEncoder.iterencode = original_iterencode

    assert hostile_called is False
    if saved:
        assert paper.PaperBook.load(path).balance == book.balance
    else:
        assert not path.exists()


def test_load_does_not_execute_self_restoring_public_json_decoder_method(
    tmp_path: Path,
) -> None:
    """A shared JSONDecoder method cannot retarget witness or snapshot parsing."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    original_raw_decode = json.JSONDecoder.raw_decode
    hostile_called = False

    def hostile_raw_decode(self, value, idx=0):
        nonlocal hostile_called
        hostile_called = True
        # Self-restoration deliberately defeats a check that only compares the public
        # class descriptor after the trusted load has already consumed it.
        json.JSONDecoder.raw_decode = original_raw_decode
        return original_raw_decode(self, value, idx)

    json.JSONDecoder.raw_decode = hostile_raw_decode
    try:
        loaded = _load_or_fail_closed(path)
    finally:
        json.JSONDecoder.raw_decode = original_raw_decode

    assert hostile_called is False
    if loaded is not None:
        assert loaded.balance == book.balance
