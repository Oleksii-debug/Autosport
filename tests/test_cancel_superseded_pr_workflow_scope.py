from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.cancel_superseded_pr_workflow_runs import CancellationError, WorkflowRun
from scripts.cancel_superseded_pr_workflow_runs_scoped import WorkflowScopedGitHubApi


HEAD = "a" * 40
STALE_HEAD = "b" * 40
_SCRIPT = Path("scripts/cancel_superseded_pr_workflow_runs_scoped.py")


class FakeScopedApi(WorkflowScopedGitHubApi):
    def __init__(self, workflow_id: int, responses: list[object]) -> None:
        super().__init__(
            repository="Oleksii-debug/Autosport",
            token="test-token",
            workflow_id=workflow_id,
        )
        self.responses = list(responses)
        self.paths: list[str] = []

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert method == "GET"
        assert not allowed_http_errors
        self.paths.append(path)
        if not self.responses:
            raise AssertionError("unexpected API request")
        return self.responses.pop(0)


def _run(
    run_id: int,
    *,
    status: str = "queued",
    pull_requests: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": run_id,
        "head_sha": HEAD,
        "name": "CI",
        "status": status,
        "pull_requests": (
            [{"number": 2022}] if pull_requests is None else pull_requests
        ),
    }


def _candidate(
    run_id: int,
    *,
    head_sha: str = HEAD,
    workflow_name: str = "CI",
    pr_numbers: tuple[int, ...] = (),
) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        head_sha=head_sha,
        workflow_name=workflow_name,
        pr_numbers=pr_numbers,
        status="queued",
    )


def _associated_pr(number: int, *, head_sha: str = HEAD) -> dict[str, object]:
    return {"number": number, "head": {"sha": head_sha}}


def test_controller_entrypoint_executes_with_actions_script_path() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--workflow-id" in result.stdout


def test_active_status_scan_is_scoped_to_exact_source_workflow_id() -> None:
    api = FakeScopedApi(
        356678400,
        [{"total_count": 1, "workflow_runs": [_run(91)]}],
    )

    runs = api._active_runs_for_status("queued")

    assert tuple(item.run_id for item in runs) == (91,)
    assert api.paths == [
        "/actions/workflows/356678400/runs?event=pull_request&status=queued&per_page=100&page=1"
    ]
    assert all(path != "/actions/runs" for path in api.paths)


def test_shrinking_active_collection_ends_on_short_page_without_failing() -> None:
    api = FakeScopedApi(
        356678400,
        [{"total_count": 200, "workflow_runs": [_run(92)]}],
    )

    runs = api._active_runs_for_status("queued")

    assert tuple(item.run_id for item in runs) == (92,)
    assert len(api.paths) == 1


def test_workflow_scope_is_positive_exact_integer() -> None:
    with pytest.raises(CancellationError, match="invalid workflow id"):
        FakeScopedApi(0, [])
    with pytest.raises(CancellationError, match="invalid workflow id"):
        FakeScopedApi(True, [])


def test_scoped_scan_keeps_strict_active_status_validation() -> None:
    api = FakeScopedApi(356678400, [])

    with pytest.raises(CancellationError, match="invalid active workflow status"):
        api._active_runs_for_status("completed")

    assert api.paths == []


def test_same_head_empty_candidate_ref_recovers_only_after_unique_target_association() -> None:
    api = FakeScopedApi(356678400, [[_associated_pr(2022)]])

    api.configure_same_head_candidate_recovery(
        pr_number=2022,
        event_head_sha=HEAD,
        workflow_name="CI",
        current_run_id=100,
    )

    recovered = api._recover_candidate_run_reference(_candidate(99))

    assert recovered.pr_numbers == (2022,)
    assert api.paths == [f"/commits/{HEAD}/pulls?per_page=100&page=1"]


def test_scoped_scan_applies_authorized_same_head_empty_reference_recovery() -> None:
    api = FakeScopedApi(
        356678400,
        [
            [_associated_pr(2022)],
            {"total_count": 1, "workflow_runs": [_run(99, pull_requests=[])]},
        ],
    )
    api.configure_same_head_candidate_recovery(
        pr_number=2022,
        event_head_sha=HEAD,
        workflow_name="CI",
        current_run_id=100,
    )

    runs = api._active_runs_for_status("queued")

    assert len(runs) == 1
    assert runs[0].run_id == 99
    assert runs[0].pr_numbers == (2022,)
    assert api.paths == [
        f"/commits/{HEAD}/pulls?per_page=100&page=1",
        "/actions/workflows/356678400/runs?event=pull_request&status=queued&per_page=100&page=1",
    ]


def test_recovered_candidate_identity_is_revalidated_at_cancel_boundary() -> None:
    api = FakeScopedApi(
        356678400,
        [
            [_associated_pr(2022)],
            [_associated_pr(2022), _associated_pr(3030)],
        ],
    )
    api.configure_same_head_candidate_recovery(
        pr_number=2022,
        event_head_sha=HEAD,
        workflow_name="CI",
        current_run_id=100,
    )
    recovered = api._recover_candidate_run_reference(_candidate(99))
    assert recovered.pr_numbers == (2022,)

    with pytest.raises(
        CancellationError,
        match="association is no longer unique",
    ):
        api.cancel(99)

    assert api.paths == [
        f"/commits/{HEAD}/pulls?per_page=100&page=1",
        f"/commits/{HEAD}/pulls?per_page=100&page=1",
    ]


@pytest.mark.parametrize(
    "candidate",
    (
        _candidate(100),
        _candidate(99, head_sha=STALE_HEAD),
        _candidate(99, workflow_name="Windows candidate"),
        _candidate(99, pr_numbers=(2022,)),
    ),
)
def test_same_head_candidate_recovery_never_expands_current_stale_foreign_or_bound_runs(
    candidate: WorkflowRun,
) -> None:
    api = FakeScopedApi(356678400, [[_associated_pr(2022)]])
    api.configure_same_head_candidate_recovery(
        pr_number=2022,
        event_head_sha=HEAD,
        workflow_name="CI",
        current_run_id=100,
    )

    assert api._recover_candidate_run_reference(candidate) is candidate


@pytest.mark.parametrize(
    "associated",
    (
        [_associated_pr(3030)],
        [_associated_pr(2022), _associated_pr(3030)],
        [],
    ),
)
def test_empty_candidate_recovery_fails_closed_for_foreign_ambiguous_or_missing_association(
    associated: list[dict[str, object]],
) -> None:
    api = FakeScopedApi(356678400, [associated])

    api.configure_same_head_candidate_recovery(
        pr_number=2022,
        event_head_sha=HEAD,
        workflow_name="CI",
        current_run_id=100,
    )

    candidate = _candidate(99)
    assert api._recover_candidate_run_reference(candidate) is candidate
