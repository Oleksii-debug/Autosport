from __future__ import annotations

from pathlib import Path


CI = Path(".github/workflows/ci.yml")
WINDOWS = Path(".github/workflows/windows-build.yml")


def test_same_head_dedup_has_bounded_actions_read_authority_only_in_ci() -> None:
    ci = CI.read_text(encoding="utf-8")
    windows = WINDOWS.read_text(encoding="utf-8")

    assert "  actions: read\n" in ci
    assert "  actions: write\n" not in ci
    assert "--dedupe-same-head" in ci

    # Windows admission only checks live-head currentness. It therefore does not need
    # repository Actions enumeration authority and keeps the narrower token surface.
    assert "  actions: read\n" not in windows
    assert "  actions: write\n" not in windows
    assert "--dedupe-same-head" not in windows
