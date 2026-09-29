from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlencode

if __package__:
    from scripts.cancel_superseded_pr_workflow_runs import (
        _ACTIVE_STATUSES,
        _RUNS_PER_PAGE,
        CancellationError,
        GitHubApi,
        WorkflowRun,
        _require_positive_int,
        cancel_superseded,
        parse_run,
    )
else:
    # GitHub Actions executes this file directly as
    # `python scripts/cancel_superseded_pr_workflow_runs_scoped.py`. In that mode
    # Python puts the scripts directory, not the repository root, first on sys.path.
    # Import the canonical sibling module without requiring package resolution.
    from cancel_superseded_pr_workflow_runs import (
        _ACTIVE_STATUSES,
        _RUNS_PER_PAGE,
        CancellationError,
        GitHubApi,
        WorkflowRun,
        _require_positive_int,
        cancel_superseded,
        parse_run,
    )


class WorkflowScopedGitHubApi(GitHubApi):
    """Trusted controller API narrowed to one exact source workflow id.

    The workflow_run trigger already identifies the source workflow. Querying the
    repository-wide Actions run collection for every active status makes each trusted
    cancellation decision scale with unrelated queue pressure and increases the chance
    that pagination races while the queue is changing. Keep the existing cancellation
    authority and live-PR rechecks, but enumerate only runs belonging to the exact source
    workflow that triggered this controller invocation.
    """

    def __init__(
        self,
        *,
        repository: str,
        token: str,
        workflow_id: int,
    ) -> None:
        super().__init__(repository=repository, token=token)
        self._workflow_id = _require_positive_int(workflow_id, field="workflow id")

    def _active_runs_for_status(self, status: str) -> tuple[WorkflowRun, ...]:
        if status not in _ACTIVE_STATUSES:
            raise CancellationError("invalid active workflow status")
        runs: list[WorkflowRun] = []
        page = 1
        while True:
            query = urlencode(
                {
                    "event": "pull_request",
                    "status": status,
                    "per_page": _RUNS_PER_PAGE,
                    "page": page,
                }
            )
            payload = self._request(
                f"/actions/workflows/{self._workflow_id}/runs?{query}"
            )
            if (
                not isinstance(payload, dict)
                or type(payload.get("total_count")) is not int
                or payload["total_count"] < 0
                or not isinstance(payload.get("workflow_runs"), list)
            ):
                raise CancellationError("invalid workflow-runs response")
            page_runs = payload["workflow_runs"]
            runs.extend(parse_run(item) for item in page_runs)
            total_count = payload["total_count"]
            # Active-run collections are inherently moving while a controller scans
            # them. A short page is therefore a safe terminal snapshot even when the
            # earlier total_count was larger. Missing a concurrently transitioned run
            # can only defer cleanup; it cannot grant cancellation authority. Do not
            # turn harmless queue shrinkage into a failed controller invocation.
            if (
                not page_runs
                or len(page_runs) < _RUNS_PER_PAGE
                or len(runs) >= total_count
            ):
                break
            page += 1
        return tuple(runs)

    def active_runs(self) -> tuple[WorkflowRun, ...]:
        runs: list[WorkflowRun] = []
        for status in _ACTIVE_STATUSES:
            runs.extend(self._active_runs_for_status(status))
        return tuple(runs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--event-head-sha", required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--workflow-id", type=int, required=True)
    parser.add_argument("--current-run-id", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        api = WorkflowScopedGitHubApi(
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            token=os.environ.get("GITHUB_TOKEN", ""),
            workflow_id=args.workflow_id,
        )
        pr_number = args.pr_number
        if pr_number <= 0:
            pr_number = api.associated_pr_number(args.event_head_sha)
        else:
            pr_number = _require_positive_int(pr_number, field="pull request number")
        result = cancel_superseded(
            api=api,
            pr_number=pr_number,
            event_head_sha=args.event_head_sha,
            workflow_name=args.workflow_name,
            current_run_id=args.current_run_id,
        )
    except CancellationError as exc:
        print(f"superseded-run cancellation failed: {exc}", file=sys.stderr)
        return 2
    cancelled = ",".join(str(item) for item in result.cancelled_run_ids)
    print("cancelled superseded workflow runs: " + cancelled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
