from __future__ import annotations

from pathlib import Path

import pytest

import autosport.continuous_session as session
from autosport.continuous_session import ContinuousSessionCoordinator, ContinuousSessionError
from autosport.paper import PaperBook


def test_load_book_rejects_session_global_paperbook_retarget_before_dispatch(
    tmp_path: Path,
) -> None:
    """The coordinator's late PaperBook global must not become loader authority."""

    canonical_paper_book = session.PaperBook
    paper_book_path = tmp_path / "paper_book.json"
    PaperBook("100").save(paper_book_path)
    hostile_load_calls: list[Path] = []

    class HostilePaperBook:
        @classmethod
        def load(cls, path):
            hostile_load_calls.append(Path(path))
            # Self-restore before post-load witnesses run. A secure preflight must
            # reject the changed session-global origin before this method executes.
            session.PaperBook = canonical_paper_book
            return canonical_paper_book("100")

    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    session.PaperBook = HostilePaperBook
    try:
        with pytest.raises(
            ContinuousSessionError,
            match=(
                "PaperBook.*authority changed|"
                "settlement coordinator dispatch changed|"
                "materialization dependency changed"
            ),
        ):
            coordinator._load_book()
        assert hostile_load_calls == []
    finally:
        session.PaperBook = canonical_paper_book
