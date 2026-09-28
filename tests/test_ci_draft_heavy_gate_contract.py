from pathlib import Path


_ACTIVITY_TYPES = (
    "types: [opened, synchronize, reopened, ready_for_review, converted_to_draft, closed]"
)
_PR_INTEGRATION_GATE = (
    "github.event.action != 'closed' && github.event.pull_request.draft == false"
)


def _workflow(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _assert_head_partitioned_pr_scheduler(path: str) -> None:
    workflow = _workflow(path)
    concurrency_block = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]

    assert "cancel-in-progress: true" in concurrency_block
    assert "format('pr-{0}-{1}'" in concurrency_block
    assert "github.run_attempt != 1" in concurrency_block
    assert "format('rerun-{0}', github.event.pull_request.head.sha)" in concurrency_block
    assert "format('qualify-{0}', github.event.pull_request.head.sha)" in concurrency_block
    assert "github.event.action == 'converted_to_draft'" in concurrency_block
    assert "github.event.action == 'closed'" in concurrency_block
    assert "'lifecycle'" in concurrency_block
    assert "github.run_id" not in concurrency_block
    assert "format('pr-{0}-run-{1}'" not in concurrency_block


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


def test_ci_pr_scheduler_coalesces_only_same_head_qualification() -> None:
    _assert_head_partitioned_pr_scheduler(".github/workflows/ci.yml")


def test_windows_pr_scheduler_keeps_per_run_isolation_until_safe_successor() -> None:
    workflow = _workflow(".github/workflows/windows-build.yml")
    concurrency_block = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]

    assert "cancel-in-progress: true" in concurrency_block
    assert "github.run_id" in concurrency_block
    assert "format('pr-{0}-run-{1}'" in concurrency_block
    assert "format('qualify-{0}', github.event.pull_request.head.sha)" not in concurrency_block


def test_endurance_pr_scheduler_coalesces_only_same_head_qualification() -> None:
    _assert_head_partitioned_pr_scheduler(".github/workflows/endurance.yml")
