from __future__ import annotations

from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
import autosport.paper as paper


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
    """Frozen facade backing is read-only while the admitted snapshot remains loadable."""

    path, book = _saved_book(tmp_path)
    surface = getattr(guard, surface_name)
    values = surface._values
    original = values[member_name]

    with pytest.raises(TypeError):
        values[member_name] = object()

    assert values[member_name] is original
    loaded = paper.PaperBook.load(path)
    assert loaded.balance == book.balance
