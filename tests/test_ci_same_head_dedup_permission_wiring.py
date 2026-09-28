from __future__ import annotations

from pathlib import Path


CI = Path(".github/workflows/ci.yml")
WINDOWS = Path(".github/workflows/windows-build.yml")


def _concurrency_block(text: str) -> str:
    return text.split("concurrency:", 1)[1].split("jobs:", 1)[0]


def test_ci_same_head_qualification_is_bounded_before_read_only_admission() -> None:
    ci = CI.read_text(encoding="utf-8")
    concurrency = _concurrency_block(ci)

    # Same-head qualification churn may coalesce before runner allocation, but a
    # different head SHA and lifecycle transition must never share that scheduler key.
    assert "format('qualify-{0}', github.event.pull_request.head.sha)" in concurrency
    assert "format('rerun-{0}', github.event.pull_request.head.sha)" in concurrency
    assert "github.event.action == 'converted_to_draft'" in concurrency
    assert "github.event.action == 'closed'" in concurrency
    assert "'lifecycle'" in concurrency
    assert "github.run_id" not in concurrency

    # PR-head code retains read-only authority. Live head + open/draft eligibility is
    # re-resolved by the admission helper before any heavy matrix can allocate.
    assert "--admission-only" in ci
    assert "--dedupe-same-head" not in ci
    assert "  actions: read\n" not in ci
    assert "  actions: write\n" not in ci


def test_windows_keeps_narrow_existing_admission_authority() -> None:
    windows = WINDOWS.read_text(encoding="utf-8")
    concurrency = _concurrency_block(windows)

    assert "format('qualify-{0}', github.event.pull_request.head.sha)" in concurrency
    assert "format('rerun-{0}', github.event.pull_request.head.sha)" in concurrency
    assert "'lifecycle'" in concurrency
    assert "  actions: read\n" not in windows
    assert "  actions: write\n" not in windows
    assert "--dedupe-same-head" not in windows
