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
        _require_sha,
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
        _require_sha,
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

    The workflow id is the canonical source-workflow identity. Historical Actions runs
    can retain an older display name after a workflow rename, so display-name drift must
    not make an exact-id member invisible to the existing canonical selector. Runs from
    the exact workflow-id endpoint are normalized to the current event display name only
    for that selector; this does not widen enumeration beyond the exact workflow id.

    GitHub may also clear ``pull_requests`` on source runs for merged/closed lifecycle
    events. The optional recovery context below is intentionally narrower than ordinary
    run parsing: it can restore one missing candidate PR reference only for the exact
    same-head lifecycle that was independently associated to the target PR. Stale-head,
    current-run, foreign-workflow, ambiguous and unassociated runs remain untrusted.
    """

    def __init__(
        self,
        *,
        repository: str,
        token: str,
        workflow_id: int,
        workflow_name: str,
    ) -> None:
        super().__init__(repository=repository, token=token)
        self._workflow_id = _require_positive_int(workflow_id, field="workflow id")
        if not isinstance(workflow_name, str) or not workflow_name:
            raise CancellationError("workflow name is required")
        self._workflow_name = workflow_name
        self._recovery_pr_number: int | None = None
        self._recovery_head_sha: str | None = None
        self._recovery_workflow_name: str | None = None
        self._recovery_current_run_id: int | None = None
        self._recovered_run_ids: set[int] = set()

    def configure_same_head_candidate_recovery(
        self,
        *,
        pr_number: int,
        event_head_sha: str,
        workflow_name: str,
        current_run_id: int,
    ) -> None:
        """Authorize exact-head empty-reference recovery only after unique association.

        Association failure is deliberately not a controller failure. It grants zero
        recovery authority, leaving the candidate's empty PR reference untouched.
        Canonical cancellation still re-resolves the live PR qualification before each
        irreversible POST; this method only repairs missing candidate identity metadata.
        """

        pr_number = _require_positive_int(pr_number, field="pull request number")
        event_head_sha = _require_sha(event_head_sha, field="event head sha")
        current_run_id = _require_positive_int(current_run_id, field="current run id")
        if workflow_name != self._workflow_name:
            raise CancellationError("workflow name does not match exact workflow id")

        self._recovery_pr_number = None
        self._recovery_head_sha = None
        self._recovery_workflow_name = None
        self._recovery_current_run_id = None
        self._recovered_run_ids.clear()
        try:
            associated_pr_number = self.associated_pr_number(event_head_sha)
        except CancellationError:
            return
        if associated_pr_number != pr_number:
            return

        self._recovery_pr_number = pr_number
        self._recovery_head_sha = event_head_sha
        self._recovery_workflow_name = workflow_name
        self._recovery_current_run_id = current_run_id

    def _canonicalize_workflow_identity(self, run: WorkflowRun) -> WorkflowRun:
        if run.workflow_name == self._workflow_name:
            return run
        return WorkflowRun(
            run_id=run.run_id,
            head_sha=run.head_sha,
            workflow_name=self._workflow_name,
            pr_numbers=run.pr_numbers,
            status=run.status,
        )

    def _recover_candidate_run_reference(self, run: WorkflowRun) -> WorkflowRun:
        pr_number = self._recovery_pr_number
        head_sha = self._recovery_head_sha
        workflow_name = self._recovery_workflow_name
        current_run_id = self._recovery_current_run_id
        if (
            pr_number is None
            or head_sha is None
            or workflow_name is None
            or current_run_id is None
            or run.pr_numbers
            or run.run_id == current_run_id
            or run.workflow_name != workflow_name
            or run.head_sha != head_sha
        ):
            return run
        self._recovered_run_ids.add(run.run_id)
        return WorkflowRun(
            run_id=run.run_id,
            head_sha=run.head_sha,
            workflow_name=run.workflow_name,
            pr_numbers=(pr_number,),
            status=run.status,
        )

    def cancel(self, run_id: int) -> None:
        """Revalidate synthetic candidate identity at the irreversible boundary."""

        run_id = _require_positive_int(run_id, field="run id")
        if run_id in self._recovered_run_ids:
            pr_number = self._recovery_pr_number
            head_sha = self._recovery_head_sha
            if pr_number is None or head_sha is None:
                raise CancellationError("recovered workflow run identity is unavailable")
            try:
                associated_pr_number = self.associated_pr_number(head_sha)
            except CancellationError as exc:
                raise CancellationError(
                    "recovered workflow run pull request association is no longer unique"
                ) from exc
            if associated_pr_number != pr_number:
                raise CancellationError(
                    "recovered workflow run pull request association changed"
                )
        super().cancel(run_id)

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
            runs.extend(
                self._recover_candidate_run_reference(
                    self._canonicalize_workflow_identity(parse_run(item))
                )
                for item in page_runs
            )
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
            workflow_name=args.workflow_name,
        )
        pr_number = args.pr_number
        if pr_number <= 0:
            pr_number = api.associated_pr_number(args.event_head_sha)
        else:
            pr_number = _require_positive_int(pr_number, field="pull request number")

        # Only a live same-head lifecycle (closed or draft) may recover missing PR
        # references on candidate source runs. If the PR becomes integration-capable
        # before canonical cancellation selects candidates, same-head selection is
        # disabled there; if its head changes, canonical cancellation returns stale.
        qualification = api.live_pr_qualification(pr_number)
        event_head_sha = _require_sha(args.event_head_sha, field="event head sha")
        if (
            qualification.head_sha == event_head_sha
            and not qualification.integration_capable
        ):
            api.configure_same_head_candidate_recovery(
                pr_number=pr_number,
                event_head_sha=event_head_sha,
                workflow_name=args.workflow_name,
                current_run_id=args.current_run_id,
            )

        result = cancel_superseded(
            api=api,
            pr_number=pr_number,
            event_head_sha=event_head_sha,
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
