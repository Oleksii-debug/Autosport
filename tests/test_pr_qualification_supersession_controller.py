from __future__ import annotations

from pathlib import Path


_WORKFLOW = Path(".github/workflows/pr-qualification-supersession.yml")


def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def test_supersession_controller_is_default_branch_owned_and_write_bounded() -> None:
    workflow = _text()

    assert "workflow_run:" in workflow
    assert "workflows: [CI, Windows candidate, Endurance]" in workflow
    assert "types: [requested]" in workflow
    assert "actions: write" in workflow
    assert "contents: read" in workflow
    assert "pull-requests: read" in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "github.event.pull_request.head.sha" not in workflow
    assert "persist-credentials: false" in workflow


def test_current_head_request_cancels_only_obsolete_same_pr_workflow_runs() -> None:
    workflow = _text()

    assert "github.event.workflow_run.event == 'pull_request'" in workflow
    assert "github.event.workflow_run.pull_requests[0].number" in workflow
    assert "github.event.workflow_run.head_sha" in workflow
    assert "github.event.workflow_run.name" in workflow
    assert "github.event.workflow_run.workflow_id" in workflow
    assert "github.event.workflow_run.id" in workflow
    assert "python scripts/cancel_superseded_pr_workflow_runs_scoped.py" in workflow
    assert '--workflow-id "${{ github.event.workflow_run.workflow_id }}"' in workflow
    assert "--admission-only" not in workflow


def test_controller_coalesces_only_same_pr_same_head_same_workflow_decision() -> None:
    workflow = _text()

    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "group: pr-qualification-supersession-" in concurrency
    assert "github.event.workflow_run.pull_requests[0].number" in concurrency
    assert "github.event.workflow_run.head_sha" in concurrency
    assert "github.event.workflow_run.workflow_id" in concurrency
    assert "cancel-in-progress: true" in concurrency
    assert "fresh live" in workflow
    assert "head/state/draft" in workflow


def test_delayed_stale_head_controller_cannot_preempt_current_head_controller() -> None:
    workflow = _text()

    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "github.event.workflow_run.head_sha" in concurrency
    assert "delayed stale-head workflow_run event must never evict" in workflow
    assert "same-head latest-wins" in workflow


def test_empty_ref_controller_is_run_unique_until_pr_identity_is_resolved() -> None:
    workflow = _text()

    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "format('unresolved-run-{0}', github.event.workflow_run.id)" in concurrency
    assert "github.event.workflow_run.head_sha" in concurrency
    assert "two distinct PRs may point" in workflow
    assert "empty-reference controller run-unique" in workflow


def test_controller_does_not_skip_close_merge_run_when_nested_pr_list_is_empty() -> None:
    workflow = _text()
    job = workflow.split("jobs:", 1)[1]

    assert "if: github.event.workflow_run.event == 'pull_request'" in job
    assert "pull_requests[0].number != null" not in job
    assert '--pr-number "${{ github.event.workflow_run.pull_requests[0].number || 0 }}"' in job
    assert "missing PR" in workflow
    assert "commit association" in workflow


def test_controller_does_not_cross_cancel_other_source_workflow_controllers() -> None:
    workflow = _text()

    assert "Controllers for CI, Windows candidate, and Endurance must not preempt one" in workflow
    assert "another" in workflow
    assert "each invocation cancels only obsolete runs of its own source workflow" in workflow
    assert "exact workflow_id carried by workflow_run" in workflow
