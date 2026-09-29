from pathlib import Path


_ACTIVITY_TYPES = (
    "types: [opened, synchronize, reopened, ready_for_review, converted_to_draft, closed]"
)
_PR_INTEGRATION_GATE = (
    "github.event.action != 'closed' && github.event.pull_request.draft == false"
)


def _workflow(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_ci_heavy_matrix_is_deferred_for_stale_draft_or_closed_pull_request() -> None:
    workflow = _workflow(".github/workflows/ci.yml")

    assert _ACTIVITY_TYPES in workflow
    assert _PR_INTEGRATION_GATE in workflow
    assert "needs.superseded_run_admission.outputs.current_head == 'true'" in workflow
    assert "--admission-only" in workflow
    assert "matrix:" in workflow
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert "python-version: ['3.11', '3.12']" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "timeout-minutes: 60" in workflow
    assert "python -m pytest -v tests" in workflow
    assert "python -m autosport demo" in workflow


def test_windows_candidate_is_deferred_for_stale_draft_or_closed_pull_request() -> None:
    workflow = _workflow(".github/workflows/windows-build.yml")

    assert _ACTIVITY_TYPES in workflow
    assert _PR_INTEGRATION_GATE in workflow
    assert "needs.superseded_run_admission.outputs.current_head == 'true'" in workflow
    assert "--admission-only" in workflow
    assert "name: Windows candidate" in workflow
    assert "runs-on: windows-latest" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "timeout-minutes: 60" in workflow
    assert "./scripts/build_windows_candidate.ps1" in workflow
    assert "Execute packaged NVDA evidence contract" in workflow
    assert "External UIA fresh-extraction gate" in workflow


def test_endurance_matrix_is_deferred_only_while_pull_request_is_draft() -> None:
    workflow = _workflow(".github/workflows/endurance.yml")

    assert _ACTIVITY_TYPES in workflow
    assert _PR_INTEGRATION_GATE in workflow
    assert "name: Endurance" in workflow
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "timeout-minutes: 20" in workflow
    assert "python -m autosport endurance" in workflow
    assert "tests/test_collector_endurance_composition.py" in workflow
    assert "Upload endurance evidence" in workflow


def test_ci_pr_scheduler_isolates_lifecycle_events_until_live_head_admission() -> None:
    workflow = _workflow(".github/workflows/ci.yml")
    assert "cancel-in-progress: true" in workflow
    assert "github.run_id" in workflow
    assert "format('pr-{0}-run-{1}'" in workflow
    assert "github.run_attempt == 1 && 'fresh'" not in workflow


def test_windows_pr_scheduler_isolates_lifecycle_events_until_live_head_admission() -> None:
    workflow = _workflow(".github/workflows/windows-build.yml")
    assert "cancel-in-progress: true" in workflow
    assert "github.run_id" in workflow
    assert "format('pr-{0}-run-{1}'" in workflow
    assert "github.run_attempt == 1 && 'fresh'" not in workflow


def test_endurance_pr_scheduler_isolates_lifecycle_events_until_live_head_admission() -> None:
    workflow = _workflow(".github/workflows/endurance.yml")

    concurrency_block = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "cancel-in-progress: true" in concurrency_block
    assert "github.run_id" in concurrency_block
    assert "format('pr-{0}-run-{1}'" in concurrency_block
    assert "github.run_attempt == 1" not in concurrency_block
    assert "'fresh'" not in concurrency_block
    assert "github.event.pull_request.number || github.ref" not in concurrency_block
