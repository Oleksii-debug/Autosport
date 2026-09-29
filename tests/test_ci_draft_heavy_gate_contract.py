from pathlib import Path


_ACTIVITY_TYPES = (
    "types: [opened, synchronize, reopened, ready_for_review, converted_to_draft, closed]"
)
_PR_INTEGRATION_GATE = (
    "github.event.action != 'closed' && github.event.pull_request.draft == false"
)
_RUNNER_FREE_PR_ADMISSION = (
    "  superseded_run_admission:\n"
    "    # Draft/closed PR lifecycle runs are already non-integration-capable. Skip this job\n"
    "    # at server-side job evaluation so they do not reserve an Ubuntu admission runner.\n"
    "    # ready_for_review has draft=false and still executes exact-head admission.\n"
    "    if: >-\n"
    "      github.event_name != 'pull_request' ||\n"
    "      (github.event.action != 'closed' && github.event.pull_request.draft == false)\n"
    "    runs-on: ubuntu-latest"
)


def _workflow(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _assert_head_partitioned_pr_scheduler(path: str) -> None:
    workflow = _workflow(path)
    concurrency_block = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]

    assert "cancel-in-progress: true" in concurrency_block
    assert "format('pr-{0}-{1}'" in concurrency_block
    assert "github.event.number" in concurrency_block
    assert "github.event.pull_request.number" not in concurrency_block
    assert "github.run_attempt != 1" in concurrency_block
    assert "format('rerun-{0}', github.event.pull_request.head.sha)" in concurrency_block
    assert "format('qualify-{0}', github.event.pull_request.head.sha)" in concurrency_block
    assert "github.event.action == 'converted_to_draft'" in concurrency_block
    assert "github.event.action == 'closed'" in concurrency_block
    assert "github.event.pull_request.draft == true" in concurrency_block
    assert "'lifecycle'" in concurrency_block
    assert "format('pr-{0}-run-{1}'" not in concurrency_block

    # Non-PR workflow activity has no trustworthy PR/head identity. Keep it run-unique
    # rather than coalescing on github.ref, which could pre-cancel same-ref work before
    # admission or product qualification executes.
    assert "|| format('run-{0}', github.run_id)" in concurrency_block
    assert "github.ref" not in concurrency_block

    # Closed, converted-to-draft, and any already-draft PR activity must coalesce
    # before exact-head rerun/qualification classification. This prevents each
    # synchronize commit on a draft PR from allocating another runner-side admission
    # run while preserving exact-head isolation as soon as the PR becomes ready.
    lifecycle_index = concurrency_block.index("github.event.action == 'converted_to_draft'")
    draft_index = concurrency_block.index("github.event.pull_request.draft == true")
    rerun_index = concurrency_block.index("github.run_attempt != 1")
    qualify_index = concurrency_block.index("format('qualify-{0}', github.event.pull_request.head.sha)")
    assert lifecycle_index < rerun_index
    assert draft_index < rerun_index
    assert draft_index < qualify_index


def test_ci_heavy_matrix_is_deferred_for_stale_draft_or_closed_pull_request() -> None:
    workflow = _workflow(".github/workflows/ci.yml")

    assert _ACTIVITY_TYPES in workflow
    assert _PR_INTEGRATION_GATE in workflow
    assert _RUNNER_FREE_PR_ADMISSION in workflow
    assert "needs.superseded_run_admission.outputs.current_head == 'true'" in workflow
    assert "--admission-only" in workflow
    assert "matrix:" in workflow
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert "python-version: ['3.11', '3.12']" in workflow
    assert "cancel-in-progress: true" in workflow
    # Full Windows/Python 3.12 qualification exceeded 60 minutes on exact-head #2035;
    # keep the complete matrix and give it a bounded 90-minute execution ceiling.
    assert "timeout-minutes: 90" in workflow
    assert "python -m pytest -v tests" in workflow
    assert "python -m autosport demo" in workflow


def test_windows_candidate_is_deferred_for_stale_draft_or_closed_pull_request() -> None:
    workflow = _workflow(".github/workflows/windows-build.yml")

    assert _ACTIVITY_TYPES in workflow
    assert _PR_INTEGRATION_GATE in workflow
    assert _RUNNER_FREE_PR_ADMISSION in workflow
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
    assert _RUNNER_FREE_PR_ADMISSION in workflow
    assert "name: Endurance" in workflow
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "timeout-minutes: 20" in workflow
    assert "python -m autosport endurance" in workflow
    assert "tests/test_collector_endurance_composition.py" in workflow
    assert "Upload endurance evidence" in workflow


def test_ci_pr_scheduler_coalesces_safe_qualification_and_draft_classes() -> None:
    _assert_head_partitioned_pr_scheduler(".github/workflows/ci.yml")


def test_windows_pr_scheduler_coalesces_safe_qualification_and_draft_classes() -> None:
    _assert_head_partitioned_pr_scheduler(".github/workflows/windows-build.yml")


def test_endurance_pr_scheduler_coalesces_safe_qualification_and_draft_classes() -> None:
    _assert_head_partitioned_pr_scheduler(".github/workflows/endurance.yml")


def test_qualification_workflows_have_repository_wide_distinct_group_prefixes() -> None:
    blocks = {
        path: _workflow(path).split("concurrency:", 1)[1].split("jobs:", 1)[0]
        for path in (
            ".github/workflows/ci.yml",
            ".github/workflows/windows-build.yml",
            ".github/workflows/endurance.yml",
        )
    }

    assert "group: ci-${{ github.workflow }}-" in blocks[".github/workflows/ci.yml"]
    assert "group: windows-candidate-" in blocks[".github/workflows/windows-build.yml"]
    assert "group: endurance-" in blocks[".github/workflows/endurance.yml"]
