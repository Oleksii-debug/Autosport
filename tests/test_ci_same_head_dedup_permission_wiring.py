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

    # Every PR run gets a scheduler-unique key so GitHub cannot cancel an older useful
    # exact-head run before the lightweight admission job applies oldest-active-wins.
    assert "github.run_id" in ci
    assert "format('pr-{0}-run-{1}'" in ci
    assert "github.run_attempt == 1 && 'fresh'" not in ci

    # Windows admission currently only checks live-head currentness. It therefore does
    # not need repository Actions enumeration authority; this carrier changes no
    # Windows token surface while fixing the observed full-CI cancellation path.
    assert "  actions: read\n" not in windows
    assert "  actions: write\n" not in windows
    assert "--dedupe-same-head" not in windows
