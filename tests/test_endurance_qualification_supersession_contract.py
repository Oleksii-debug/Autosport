from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> str:
    return (_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_endurance_pr_runs_are_head_partitioned_until_live_admission() -> None:
    text = _workflow("endurance.yml")

    assert "types: [opened, synchronize, reopened, ready_for_review, converted_to_draft, closed]" in text
    assert "pull-requests: read" in text
    assert "superseded_run_admission:" in text
    assert "--admission-only" in text
    assert "--workflow-name \"${{ github.workflow }}\"" in text
    assert "needs: superseded_run_admission" in text
    assert "needs.superseded_run_admission.outputs.current_head == 'true'" in text
    assert "github.event.pull_request.draft == false" in text

    # Scheduler-level coalescing is safe only within the same exact event head and
    # lifecycle class. A delayed stale-head event cannot share a qualification key with
    # current head, while converted_to_draft/closed cannot evict useful qualification.
    concurrency_block = text.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "format('qualify-{0}', github.event.pull_request.head.sha)" in concurrency_block
    assert "format('rerun-{0}', github.event.pull_request.head.sha)" in concurrency_block
    assert "github.event.action == 'converted_to_draft'" in concurrency_block
    assert "github.event.action == 'closed'" in concurrency_block
    assert "'lifecycle'" in concurrency_block
    assert "github.run_id" not in concurrency_block
    assert "github.event.pull_request.number || github.ref" not in concurrency_block


def test_trusted_supersession_controller_covers_endurance() -> None:
    text = _workflow("pr-qualification-supersession.yml")

    assert "workflows: [CI, Windows candidate, Endurance]" in text
    assert "actions: write" in text
    assert "cancel-in-progress: false" in text
    assert "github.event.workflow_run.head_sha" in text
    assert "github.event.workflow_run.name" in text
