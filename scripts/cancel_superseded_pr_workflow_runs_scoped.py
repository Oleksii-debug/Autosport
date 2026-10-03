from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import quote, urlencode

if __package__:
    from scripts.cancel_superseded_pr_workflow_runs import (
        _ACTIVE_STATUSES,
        _AllowedHttpError,
        _PULLS_PER_PAGE,
        _RUNS_PER_PAGE,
        CancellationError,
        GitHubApi,
        WorkflowRun,
        _require_positive_int,
        _require_sha,
        parse_run,
        select_superseded_runs,
    )
else:
    # GitHub Actions executes this file directly as
    # `python scripts/cancel_superseded_pr_workflow_runs_scoped.py`. In that mode
    # Python puts the scripts directory, not the repository root, first on sys.path.
    # Import the canonical sibling module without requiring package resolution.
    from cancel_superseded_pr_workflow_runs import (
        _ACTIVE_STATUSES,
        _AllowedHttpError,
        _PULLS_PER_PAGE,
        _RUNS_PER_PAGE,
        CancellationError,
        GitHubApi,
        WorkflowRun,
        _require_positive_int,
        _require_sha,
        parse_run,
        select_superseded_runs,
    )


class _HistoricalAssociationAbsent(CancellationError):
    """A well-formed historical commit lookup returned no associated PR."""


class _HistoricalAssociationAmbiguous(CancellationError):
    """A well-formed historical commit lookup returned multiple PRs."""


class _CancellationAuthorityChanged(CancellationError):
    """A valid candidate lost cancellation authority during the boundary reread."""


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
        self._historical_recovery_pr_number: int | None = None
        self._historical_recovery_workflow_name: str | None = None
        self._historical_recovery_current_run_id: int | None = None
        self._recovered_runs: dict[int, tuple[int, str]] = {}
        self._unbound_active_runs: dict[int, tuple[str, str | None]] = {}
        self._zero_association_recovered_runs: dict[int, tuple[str, str]] = {}

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
        self._recovered_runs.clear()
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

    def _historical_associated_pr_number(self, head_sha: str) -> int:
        """Resolve one historical commit association without requiring current-head equality.

        This resolver is cleanup-only.  It never admits current/event authority: callers
        may use it only after the target PR has already been identified by the canonical
        current authority path.  Every page must identify exactly one distinct PR number.
        """

        head_sha = _require_sha(head_sha, field="historical workflow head sha")
        associated_numbers: set[int] = set()
        page = 1
        while True:
            query = urlencode({"per_page": _PULLS_PER_PAGE, "page": page})
            payload = self._request(f"/commits/{head_sha}/pulls?{query}")
            if not isinstance(payload, list):
                raise CancellationError(
                    "invalid historical commit pull-requests response"
                )
            for item in payload:
                if not isinstance(item, dict):
                    raise CancellationError(
                        "invalid historical associated pull request"
                    )
                head = item.get("head")
                if not isinstance(head, dict):
                    raise CancellationError(
                        "invalid historical associated pull request head"
                    )
                _require_sha(
                    head.get("sha"),
                    field="historical associated pull request head",
                )
                associated_numbers.add(
                    _require_positive_int(
                        item.get("number"),
                        field="historical associated pull request number",
                    )
                )
            if len(payload) < _PULLS_PER_PAGE:
                break
            page += 1
        if not associated_numbers:
            raise _HistoricalAssociationAbsent(
                "historical workflow head has no associated pull request"
            )
        if len(associated_numbers) != 1:
            raise _HistoricalAssociationAmbiguous(
                "historical workflow head resolves to multiple associated pull requests"
            )
        return next(iter(associated_numbers))

    def configure_historical_candidate_recovery(
        self,
        *,
        pr_number: int,
        workflow_name: str,
        current_run_id: int,
    ) -> None:
        """Allow fail-closed identity recovery for stale empty-reference candidates."""

        pr_number = _require_positive_int(pr_number, field="pull request number")
        current_run_id = _require_positive_int(current_run_id, field="current run id")
        if workflow_name != self._workflow_name:
            raise CancellationError("workflow name does not match exact workflow id")
        self._historical_recovery_pr_number = pr_number
        self._historical_recovery_workflow_name = workflow_name
        self._historical_recovery_current_run_id = current_run_id
        self._recovered_runs.clear()

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
        if run.pr_numbers:
            return run

        pr_number: int | None = None
        if (
            self._recovery_pr_number is not None
            and self._recovery_head_sha is not None
            and self._recovery_workflow_name is not None
            and self._recovery_current_run_id is not None
            and run.run_id != self._recovery_current_run_id
            and run.workflow_name == self._recovery_workflow_name
            and run.head_sha == self._recovery_head_sha
        ):
            pr_number = self._recovery_pr_number
        elif (
            self._historical_recovery_pr_number is not None
            and self._historical_recovery_workflow_name is not None
            and self._historical_recovery_current_run_id is not None
            and run.run_id != self._historical_recovery_current_run_id
            and run.workflow_name == self._historical_recovery_workflow_name
        ):
            try:
                historical_pr_number = self._historical_associated_pr_number(
                    run.head_sha
                )
            except CancellationError:
                return run
            if historical_pr_number != self._historical_recovery_pr_number:
                return run
            pr_number = historical_pr_number

        if pr_number is None:
            return run
        self._recovered_runs[run.run_id] = (pr_number, run.head_sha)
        return WorkflowRun(
            run_id=run.run_id,
            head_sha=run.head_sha,
            workflow_name=run.workflow_name,
            pr_numbers=(pr_number,),
            status=run.status,
        )

    def _historical_head_has_no_associated_prs(self, head_sha: str) -> bool:
        """Return true only for a well-formed commit association response with zero PRs."""

        head_sha = _require_sha(head_sha, field="historical workflow head sha")
        page = 1
        associated_numbers: set[int] = set()
        while True:
            query = urlencode({"per_page": _PULLS_PER_PAGE, "page": page})
            payload = self._request(f"/commits/{head_sha}/pulls?{query}")
            if not isinstance(payload, list):
                raise CancellationError(
                    "invalid historical commit pull-requests response"
                )
            for item in payload:
                if not isinstance(item, dict):
                    raise CancellationError(
                        "invalid historical associated pull request"
                    )
                associated_numbers.add(
                    _require_positive_int(
                        item.get("number"),
                        field="historical associated pull request number",
                    )
                )
            if len(payload) < _PULLS_PER_PAGE:
                break
            page += 1
        return not associated_numbers

    def _canonical_branch_head(self, branch: str) -> str | None:
        """Resolve one same-repository branch head; absence is authoritative."""

        if not isinstance(branch, str) or not branch:
            raise CancellationError("invalid canonical head branch")
        encoded = quote(branch, safe="")
        payload = self._request(
            f"/git/ref/heads/{encoded}",
            allowed_http_errors=frozenset({404}),
        )
        if isinstance(payload, _AllowedHttpError):
            if payload.status_code == 404:
                return None
        if not isinstance(payload, dict):
            raise CancellationError("invalid canonical branch response")
        target = payload.get("object")
        if not isinstance(target, dict):
            raise CancellationError("invalid canonical branch target")
        return _require_sha(target.get("sha"), field="canonical branch head")

    def _build_cancel(
        base_cancel,
        historical_associated_pr_number,
        historical_head_has_no_associated_prs,
        canonical_branch_head,
        live_pr_qualification,
        pull_request,
    ):
        base_cancel_code = getattr(base_cancel, "__code__", None)
        helper_dispatch = {
            name: (implementation, getattr(implementation, "__code__", None))
            for name, implementation in (
                (
                    "_historical_associated_pr_number",
                    historical_associated_pr_number,
                ),
                (
                    "_historical_head_has_no_associated_prs",
                    historical_head_has_no_associated_prs,
                ),
                ("_canonical_branch_head", canonical_branch_head),
                ("live_pr_qualification", live_pr_qualification),
                ("_pull_request", pull_request),
            )
        }

        def cancel(self, run_id: int) -> None:
            """Revalidate synthetic candidate identity at the irreversible boundary."""

            if getattr(base_cancel, "__code__", None) is not base_cancel_code:
                raise CancellationError(
                    "canonical base cancellation authority changed"
                )

            def require_helper_dispatch(name: str) -> None:
                expected, expected_code = helper_dispatch[name]
                bound = getattr(self, name, None)
                if (
                    getattr(expected, "__code__", None) is not expected_code
                    or getattr(bound, "__self__", None) is not self
                    or getattr(bound, "__func__", None) is not expected
                ):
                    raise CancellationError(
                        "scoped cancellation revalidation dispatch changed"
                    )

            for name in helper_dispatch:
                require_helper_dispatch(name)

            run_id = _require_positive_int(run_id, field="run id")
            zero_association = self._zero_association_recovered_runs.get(run_id)
            if zero_association is not None:
                candidate_head_sha, head_branch = zero_association
                try:
                    require_helper_dispatch("_historical_head_has_no_associated_prs")
                    no_association = historical_head_has_no_associated_prs(
                        self, candidate_head_sha
                    )
                    require_helper_dispatch("_canonical_branch_head")
                    branch_head_sha = canonical_branch_head(self, head_branch)
                except CancellationError as exc:
                    raise CancellationError(
                        "unbound workflow run branch authority could not be revalidated"
                    ) from exc
                if not no_association or branch_head_sha == candidate_head_sha:
                    raise _CancellationAuthorityChanged(
                        "unbound workflow run branch authority changed"
                    )
    
            recovered = self._recovered_runs.get(run_id)
            if recovered is not None:
                pr_number, candidate_head_sha = recovered
                try:
                    require_helper_dispatch("_historical_associated_pr_number")
                    associated_pr_number = historical_associated_pr_number(
                        self, candidate_head_sha
                    )
                except (
                    _HistoricalAssociationAbsent,
                    _HistoricalAssociationAmbiguous,
                ) as exc:
                    raise _CancellationAuthorityChanged(
                        "recovered workflow run pull request association is no longer unique"
                    ) from exc
                if associated_pr_number != pr_number:
                    raise _CancellationAuthorityChanged(
                        "recovered workflow run pull request association changed"
                    )
    
                # Association validation is an external round trip. Re-resolve current PR
                # truth immediately afterward.  A historical candidate may be cancelled
                # while the PR has advanced, but if the PR rolls back to that exact head,
                # same-head cancellation again requires a non-integration-capable lifecycle.
                require_helper_dispatch("live_pr_qualification")
                require_helper_dispatch("_pull_request")
                qualification = live_pr_qualification(self, pr_number)
                if (
                    qualification.head_sha == candidate_head_sha
                    and qualification.integration_capable
                ):
                    raise _CancellationAuthorityChanged(
                        "recovered workflow run live qualification changed"
                    )
            if getattr(base_cancel, "__code__", None) is not base_cancel_code:
                raise CancellationError(
                    "canonical base cancellation authority changed"
                )
            base_cancel(self, run_id)
    
        return cancel

    cancel = _build_cancel(
        GitHubApi.cancel,
        _historical_associated_pr_number,
        _historical_head_has_no_associated_prs,
        _canonical_branch_head,
        GitHubApi.live_pr_qualification,
        GitHubApi._pull_request,
    )
    del _build_cancel

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
            for item in page_runs:
                run = self._canonicalize_workflow_identity(parse_run(item))
                if not run.pr_numbers:
                    head_branch: str | None = None
                    if isinstance(item, dict):
                        raw_branch = item.get("head_branch")
                        head_repository = item.get("head_repository")
                        if (
                            isinstance(raw_branch, str)
                            and raw_branch
                            and isinstance(head_repository, dict)
                            and head_repository.get("full_name") == self._repository
                        ):
                            head_branch = raw_branch
                    self._unbound_active_runs[run.run_id] = (
                        run.head_sha,
                        head_branch,
                    )
                else:
                    # active_runs() queries statuses sequentially, so the same run can
                    # transition between requests. A later explicit PR observation is
                    # stronger than an earlier unbound observation for that exact run id
                    # and must revoke the stale orphan candidate before cleanup.
                    self._unbound_active_runs.pop(run.run_id, None)
                runs.append(self._recover_candidate_run_reference(run))
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


    def cancel_historical_unbound_runs(
        self,
        *,
        exclude_run_ids: tuple[int, ...] = (),
    ) -> tuple[int, ...]:
        """Cancel uniquely-associated active runs whose PR metadata disappeared.

        Some old pull_request workflow runs can remain active after GitHub clears the
        run's embedded pull_requests references. Those runs are invisible to ordinary
        PR-scoped supersession and can occupy the queue indefinitely. Cleanup is
        deliberately fail-closed: each candidate must resolve to exactly one historical
        PR, and a same-head integration-capable PR is always preserved. The recovered
        identity is recorded so cancel() repeats both the commit association and live PR
        qualification immediately before the irreversible POST.
        """

        excluded = {
            _require_positive_int(run_id, field="excluded run id")
            for run_id in exclude_run_ids
        }
        cancelled: list[int] = []
        for run_id, candidate in sorted(self._unbound_active_runs.items()):
            if run_id in excluded:
                continue
            candidate_head_sha, head_branch = candidate
            try:
                pr_number = self._historical_associated_pr_number(candidate_head_sha)
            except _HistoricalAssociationAbsent:
                if head_branch is None:
                    continue
                try:
                    if not self._historical_head_has_no_associated_prs(
                        candidate_head_sha
                    ):
                        continue
                    branch_head_sha = self._canonical_branch_head(head_branch)
                except CancellationError:
                    continue
                if branch_head_sha == candidate_head_sha:
                    continue
                self._zero_association_recovered_runs[run_id] = (
                    candidate_head_sha,
                    head_branch,
                )
                try:
                    self.cancel(run_id)
                except _CancellationAuthorityChanged:
                    self._zero_association_recovered_runs.pop(run_id, None)
                    continue
                self._zero_association_recovered_runs.pop(run_id, None)
                cancelled.append(run_id)
                continue
            except _HistoricalAssociationAmbiguous:
                continue
            except CancellationError:
                continue

            try:
                qualification = self.live_pr_qualification(pr_number)
            except CancellationError:
                continue
            if (
                qualification.head_sha == candidate_head_sha
                and qualification.integration_capable
            ):
                continue
            self._recovered_runs[run_id] = (pr_number, candidate_head_sha)
            try:
                self.cancel(run_id)
            except _CancellationAuthorityChanged:
                self._recovered_runs.pop(run_id, None)
                continue
            self._recovered_runs.pop(run_id, None)
            cancelled.append(run_id)
        return tuple(cancelled)


def _explicit_singleton_pr_for_current_run(
    runs: tuple[WorkflowRun, ...],
    *,
    workflow_name: str,
    current_run_id: int,
    event_head_sha: str,
) -> int | None:
    """Derive current source-run PR identity only from one consistent explicit snapshot.

    The workflow_run event can omit pull_requests while the exact-workflow Actions
    collection already exposes a singleton reference for the same source run. Reuse
    that trusted snapshot without inventing authority from scheduler state. If the
    moving active-run collection exposes no entry, an empty/multi-reference entry, or
    conflicting singleton identities for the current run, fail closed and return None.
    """

    current_run_id = _require_positive_int(current_run_id, field="current run id")
    event_head_sha = _require_sha(event_head_sha, field="event head sha")
    if type(workflow_name) is not str or not workflow_name:
        raise CancellationError("workflow name is required")
    current_entries = tuple(
        run
        for run in runs
        if run.run_id == current_run_id and run.workflow_name == workflow_name
    )
    if not current_entries or any(
        run.head_sha != event_head_sha or len(run.pr_numbers) != 1
        for run in current_entries
    ):
        return None
    pr_numbers = {run.pr_numbers[0] for run in current_entries}
    if len(pr_numbers) != 1:
        return None
    return _require_positive_int(
        next(iter(pr_numbers)),
        field="snapshot pull request number",
    )


def cancel_superseded_explicit_pr_runs(
    api: WorkflowScopedGitHubApi,
    *,
    workflow_name: str,
    current_run_id: int,
    runs: tuple[WorkflowRun, ...] | None = None,
) -> tuple[int, ...]:
    """Sweep superseded runs for every explicit singleton PR in one workflow snapshot.

    The exact-workflow API already performs one bounded active-run enumeration. Reuse
    that snapshot across PR groups instead of requiring one trusted controller job per
    PR. Each group still derives same-head cancellation from one live qualification
    snapshot and rereads that exact snapshot immediately before every irreversible POST.

    Empty/multi-reference runs never gain PR identity here. Historical missing-reference
    cleanup remains owned by cancel_historical_unbound_runs and keeps its existing
    association/branch boundary checks.
    """

    current_run_id = _require_positive_int(current_run_id, field="current run id")
    if (
        type(workflow_name) is not str
        or not workflow_name
        or workflow_name != api._workflow_name
    ):
        raise CancellationError("workflow name does not match exact workflow id")

    if runs is None:
        runs = api.active_runs()
    elif type(runs) is not tuple or any(
        not isinstance(run, WorkflowRun) for run in runs
    ):
        raise CancellationError("invalid exact-workflow active-run snapshot")
    explicit_singleton_runs = tuple(
        run
        for run in runs
        if run.workflow_name == workflow_name and len(run.pr_numbers) == 1
    )
    pr_numbers = sorted(
        {
            run.pr_numbers[0]
            for run in explicit_singleton_runs
            if run.run_id != current_run_id
        }
    )

    cancelled: list[int] = []
    cancelled_ids: set[int] = set()
    for pr_number in pr_numbers:
        try:
            qualification = api.live_pr_qualification(pr_number)
        except CancellationError:
            # Qualification authority is scoped to one PR group. Failure to resolve
            # one group must fail that group closed without starving independent PRs
            # whose own live authority can still be proven.
            continue
        selected = select_superseded_runs(
            explicit_singleton_runs,
            pr_number=pr_number,
            live_head_sha=qualification.head_sha,
            workflow_name=workflow_name,
            current_run_id=current_run_id,
            cancel_same_head=not qualification.integration_capable,
        )
        for run_id in selected:
            if run_id in cancelled_ids:
                continue
            # A head/state/draft move or reread failure revokes authority only for
            # this PR group. Other independently resolved groups can still progress.
            try:
                current_qualification = api.live_pr_qualification(pr_number)
            except CancellationError:
                break
            if current_qualification != qualification:
                break
            api.cancel(run_id)
            cancelled.append(run_id)
            cancelled_ids.add(run_id)
    return tuple(cancelled)


def _cancel_triggering_run_if_stale_or_nonqualifying(
    api: WorkflowScopedGitHubApi,
    *,
    pr_number: int,
    event_head_sha: str,
    current_run_id: int,
    qualification,
) -> bool:
    """Cancel a source run proven stale or same-head non-integration-capable.

    Controller concurrency may intentionally coalesce multiple source-head events for
    the same explicit PR + workflow.  Therefore the surviving controller must remain
    useful even when its triggering source run is stale.  The irreversible POST is
    allowed only after a fresh live qualification snapshot still matches the snapshot
    used for the decision.
    """

    event_head_sha = _require_sha(event_head_sha, field="event head sha")
    current_run_id = _require_positive_int(current_run_id, field="current run id")
    stale = qualification.head_sha != event_head_sha
    same_head_nonqualifying = (
        qualification.head_sha == event_head_sha
        and not qualification.integration_capable
    )
    if not stale and not same_head_nonqualifying:
        return False
    try:
        current_qualification = api.live_pr_qualification(pr_number)
    except CancellationError:
        # A failed authority reread grants no trigger cancellation authority, but it
        # must not invalidate independently completed workflow-wide cleanup.
        return False
    if current_qualification != qualification:
        return False
    api.cancel(current_run_id)
    return True


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
        event_head_sha = _require_sha(args.event_head_sha, field="event head sha")
        current_run_id = _require_positive_int(
            args.current_run_id,
            field="current run id",
        )
        if args.pr_number < 0:
            raise CancellationError("pull request number cannot be negative")

        # The triggering event's explicit singleton PR is retained only for the
        # separately boundary-revalidated triggering-source-run decision after the
        # workflow-wide sweep. A zero value means the event did not carry one
        # unambiguous PR identity; that must not block cleanup for every other explicit
        # PR in the exact source workflow. Missing-reference source runs are handled
        # uniformly by the existing workflow-wide orphan resolver after the shared
        # active-run scan, so trigger-specific pre-recovery is unnecessary.
        trigger_pr_number: int | None = None
        if args.pr_number > 0:
            trigger_pr_number = _require_positive_int(
                args.pr_number,
                field="pull request number",
            )

        # One exact-workflow snapshot now reconciles every explicit singleton PR group.
        # If the trigger event itself carried no unambiguous PR identity, retain this
        # same snapshot long enough to recover only a consistent explicit singleton
        # identity for the current source run. This closes the event/snapshot metadata
        # race without granting identity from scheduler state or historical heuristics.
        sweep_runs: tuple[WorkflowRun, ...] | None = None
        snapshot_trigger_pr_number: int | None = None
        if trigger_pr_number is None:
            sweep_runs = api.active_runs()
            snapshot_trigger_pr_number = _explicit_singleton_pr_for_current_run(
                sweep_runs,
                workflow_name=args.workflow_name,
                current_run_id=current_run_id,
                event_head_sha=event_head_sha,
            )
        sweep_cancelled = cancel_superseded_explicit_pr_runs(
            api,
            workflow_name=args.workflow_name,
            current_run_id=current_run_id,
            runs=sweep_runs,
        )
        if trigger_pr_number is None and snapshot_trigger_pr_number is not None:
            trigger_pr_number = snapshot_trigger_pr_number
        # An explicitly identified triggering source run has its own
        # live-qualification boundary below, so keep it out of orphan cleanup to avoid
        # a second cancellation race if the Actions list has meanwhile lost embedded
        # PR references. A still-zero trigger has no separate PR boundary below; when
        # the exact snapshot recorded that current run as truly unbound, the historical
        # orphan resolver is its only authorized cleanup path and must be allowed to
        # consider it. A zero event identity recovered from a same-run/same-head explicit
        # singleton snapshot uses the ordinary trigger boundary below instead.
        orphan_excluded_run_ids = (
            (current_run_id, *sweep_cancelled)
            if trigger_pr_number is not None
            else sweep_cancelled
        )
        orphan_cancelled = api.cancel_historical_unbound_runs(
            exclude_run_ids=orphan_excluded_run_ids,
        )

        if trigger_pr_number is not None:
            # Refresh after the potentially long sweep; the helper itself rereads once
            # more immediately before cancelling this exact triggering source run.
            try:
                trigger_qualification = api.live_pr_qualification(trigger_pr_number)
            except CancellationError:
                # Trigger qualification is authority for this one source run only.
                # Failure proves no cancellation authority and must not turn already
                # completed workflow-wide reconciliation into a controller failure.
                trigger_qualification = None
            if trigger_qualification is not None:
                _cancel_triggering_run_if_stale_or_nonqualifying(
                    api,
                    pr_number=trigger_pr_number,
                    event_head_sha=event_head_sha,
                    current_run_id=current_run_id,
                    qualification=trigger_qualification,
                )
    except CancellationError as exc:
        print(f"superseded-run cancellation failed: {exc}", file=sys.stderr)
        return 2
    cancelled = ",".join(str(item) for item in sweep_cancelled)
    orphaned = ",".join(str(item) for item in orphan_cancelled)
    print("cancelled superseded workflow runs: " + cancelled)
    print("cancelled historical unbound workflow runs: " + orphaned)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
