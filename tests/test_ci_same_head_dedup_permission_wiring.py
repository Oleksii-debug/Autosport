from __future__ import annotations

from pathlib import Path


CI = Path(".github/workflows/ci.yml")
WINDOWS = Path(".github/workflows/windows-build.yml")


def test_ci_pr_runs_reach_read_only_admission_before_supersession() -> None:
    ci = CI.read_text(encoding="utf-8")

    # PR scheduler groups are unique per workflow run. GitHub therefore cannot
    # pre-cancel an older useful run merely because another lifecycle event for the
    # same PR/head was emitted; the lightweight live-head gate gets to decide first.
    assert "github.run_id" in ci
    assert "format('pr-{0}-run-{1}'" in ci
    assert "github.run_attempt == 1 && 'fresh'" not in ci

    # Admission is deliberately head-only. In particular, an older draft event on
    # the same exact head must not suppress the later ready_for_review qualification.
    assert "--admission-only" in ci
    assert "--dedupe-same-head" not in ci
    assert "  actions: read\n" not in ci
    assert "  actions: write\n" not in ci


def test_windows_keeps_narrow_existing_admission_authority() -> None:
    windows = WINDOWS.read_text(encoding="utf-8")
    assert "  actions: read\n" not in windows
    assert "  actions: write\n" not in windows
    assert "--dedupe-same-head" not in windows
