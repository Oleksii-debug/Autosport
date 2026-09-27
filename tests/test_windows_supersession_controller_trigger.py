from __future__ import annotations

from pathlib import Path


def test_windows_candidate_tracks_trusted_supersession_controller_changes() -> None:
    workflow = Path(".github/workflows/windows-build.yml").read_text(encoding="utf-8")

    assert "- '.github/workflows/pr-qualification-supersession.yml'" in workflow
