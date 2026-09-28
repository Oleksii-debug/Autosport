from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> str:
    return (_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_endurance_pr_runs_are_scheduler_isolated_until_live_admission() -> None:
    text = _workflow("endurance.yml")

    assert "types: [opened, synchronize, reopened, ready_for_review, converted_to_draft, closed]" in text
    assert "pull-requests: read" in text
    assert "superseded_run_admission:" in text
    assert "--admission-only" in text
    assert "--workflow-name \"${{ github.workflow }}\"" in text
    assert "github.run_id" in text
    assert "needs: superseded_run_admission" in text
    assert "needs.superseded_run_admission.outputs.current_head == 'true'" in text
    assert "github.event.pull_request.draft == false" in text

    # The former shared per-PR group let a delayed converted_to_draft event cancel a
    # ready_for_review run on the same exact head before either could resolve live PR
    # state. Every PR run must now carry its own run id in the scheduler group.
    concurrency_block = text.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "github.run_id" in concurrency_block
    assert "github.event.pull_request.number || github.ref" not in concurrency_block


def test_trusted_supersession_controller_covers_endurance() -> None:
    text = _workflow("pr-qualification-supersession.yml")

    assert "workflows: [CI, Windows candidate, Endurance]" in text
    assert "actions: write" in text
    assert "cancel-in-progress: false" in text
    assert "github.event.workflow_run.head_sha" in text
    assert "github.event.workflow_run.name" in text
