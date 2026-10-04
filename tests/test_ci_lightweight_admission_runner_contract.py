from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _job_block(path: str, job_name: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    marker = f"  {job_name}:"
    block = text.split(marker, 1)[1]
    lines = block.splitlines()
    selected: list[str] = []
    for line in lines:
        if line.startswith("  ") and not line.startswith("    "):
            break
        selected.append(line)
    return "\n".join(selected)


def test_read_only_admission_jobs_use_lightweight_runner() -> None:
    for path in (
        ".github/workflows/ci.yml",
        ".github/workflows/windows-build.yml",
        ".github/workflows/endurance.yml",
    ):
        block = _job_block(path, "superseded_run_admission")
        assert "runs-on: ubuntu-slim" in block
        assert "timeout-minutes: 5" in block
        assert "actions/checkout@" in block
        assert "--admission-only" in block


def test_trusted_supersession_controller_uses_lightweight_runner() -> None:
    block = _job_block(
        ".github/workflows/pr-qualification-supersession.yml",
        "cancel_superseded_head_runs",
    )
    assert "runs-on: ubuntu-slim" in block
    assert "timeout-minutes: 5" in block
    assert "actions/checkout@" in block
    assert "cancel_superseded_pr_workflow_runs_scoped.py" in block


def test_heavy_qualification_jobs_keep_platform_specific_runners() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    windows = (ROOT / ".github/workflows/windows-build.yml").read_text(encoding="utf-8")
    endurance = (ROOT / ".github/workflows/endurance.yml").read_text(encoding="utf-8")

    assert "runs-on: ${{ matrix.os }}" in ci
    assert "runs-on: windows-latest" in windows
    assert "runs-on: ${{ matrix.os }}" in endurance
