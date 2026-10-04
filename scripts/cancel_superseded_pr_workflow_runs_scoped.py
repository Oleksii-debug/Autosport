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
        _pull_request_qualification_state,
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
        _pull_request_qualification_state,
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


_ACTIVE_CANCELLATION_CONFLICT_MESSAGE = (
    "workflow run cancellation conflicted while run remains active"
)


def _build_cancel_run_or_defer_active_conflict(
    error_type,
    active_conflict_message: str,
):
    """Freeze the one cancellation race that may be deferred without success."""

    if type(active_conflict_message) is not str or not active_conflict_message:
        raise RuntimeError("canonical active-cancellation conflict message is unavailable")

    def cancel_or_defer(api: GitHubApi, run_id: int) -> bool:
        """Attempt one authorized cancel without promoting a 409-active race to success.

        Canonical GitHubApi.cancel() proves HTTP 202 acceptance or completed status after
        a 409. Its exact 409-active error means this controller obtained no cancellation
        effect because another actor may already be racing the same run. Defer that run
        to a later sweep, but preserve every other cancellation error as fatal/unknown.
        """

        # This helper sits directly between exact run-identity proof and api.cancel().
        # Preserve that proven primitive coordinate; a rebound compatibility validator
        # must not be able to substitute another run after the proof has completed.
        if type(run_id) is not int or run_id <= 0:
            raise error_type("invalid run id")
        try:
            api.cancel(run_id)
        except error_type as exc:
            if (
                exc.__class__ is error_type
                and exc.args == (active_conflict_message,)
            ):
                return False
            raise
        return True

    return cancel_or_defer


_cancel_run_or_defer_active_conflict = _build_cancel_run_or_defer_active_conflict(
    CancellationError,
    _ACTIVE_CANCELLATION_CONFLICT_MESSAGE,
)
del _build_cancel_run_or_defer_active_conflict
del _ACTIVE_CANCELLATION_CONFLICT_MESSAGE


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
        if type(workflow_id) is not int or workflow_id <= 0:
            raise CancellationError("invalid workflow id")
        canonical_workflow_id = workflow_id
        if not isinstance(workflow_name, str) or not workflow_name:
            raise CancellationError("workflow name is required")
        # Keep the source-workflow trust root separate from compatibility aliases.
        # The trusted Actions enumeration and run-identity boundaries snapshot these
        # private coordinates and fail closed if they drift across an external read.
        self.__workflow_id = canonical_workflow_id
        self.__workflow_name = workflow_name
        self._workflow_id = canonical_workflow_id
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
        self._explicit_active_run_ids: set[int] = set()
        self._conflicted_unbound_run_ids: set[int] = set()
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
        if workflow_name != object.__getattribute__(
            self,
            "_WorkflowScopedGitHubApi__workflow_name",
        ):
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

    def _historical_associated_pr_number(
        self,
        head_sha: str,
        *,
        _request_impl=GitHubApi._request,
        _request_code=GitHubApi._request.__code__,
        _request_defaults=GitHubApi._request.__defaults__,
        _request_kwdefaults=GitHubApi._request.__kwdefaults__,
        _request_kwdefault_items=tuple(GitHubApi._request.__kwdefaults__.items()),
        _pulls_per_page: int = _PULLS_PER_PAGE,
        _encode_query=urlencode,
        _encode_query_code=urlencode.__code__,
        _encode_query_defaults=urlencode.__defaults__,
        _encode_query_kwdefaults=urlencode.__kwdefaults__,
        _error_type=CancellationError,
        _absent_type=_HistoricalAssociationAbsent,
        _ambiguous_type=_HistoricalAssociationAmbiguous,
    ) -> int:
        """Resolve one historical commit association without requiring current-head equality.

        This resolver is cleanup-only. It never admits current/event authority: callers
        may use it only after the target PR has already been identified by the canonical
        current authority path. Every page must identify exactly one distinct PR number.

        The request executable, pagination bound, query encoder, primitive validators and
        exception classes are frozen at function-definition time. A canonical HTTP read
        may execute arbitrary runtime callbacks; none of those callbacks may redirect a
        later page, rewrite provider PR identity, rewrite the commit coordinate, or widen
        a provider-capped page size so a real second page is silently skipped.
        """

        def require_sha_primitive(value: object, *, field: str) -> str:
            if type(value) is not str or len(value) != 40:
                raise _error_type(f"invalid {field}")
            canonical = value.lower()
            if any(ch not in "0123456789abcdef" for ch in canonical):
                raise _error_type(f"invalid {field}")
            return canonical

        def require_positive_int_primitive(value: object, *, field: str) -> int:
            if type(value) is not int or value <= 0:
                raise _error_type(f"invalid {field}")
            return value

        def request_dispatch_current() -> bool:
            bound = getattr(self, "_request", None)
            return (
                getattr(_request_impl, "__code__", None) is _request_code
                and getattr(_request_impl, "__defaults__", None)
                is _request_defaults
                and getattr(_request_impl, "__kwdefaults__", None)
                is _request_kwdefaults
                and len(_request_kwdefaults) == len(_request_kwdefault_items)
                and all(
                    key in _request_kwdefaults
                    and _request_kwdefaults[key] is value
                    for key, value in _request_kwdefault_items
                )
                and getattr(bound, "__self__", None) is self
                and getattr(bound, "__func__", None) is _request_impl
                and getattr(_encode_query, "__code__", None) is _encode_query_code
                and getattr(_encode_query, "__defaults__", None)
                is _encode_query_defaults
                and getattr(_encode_query, "__kwdefaults__", None)
                is _encode_query_kwdefaults
            )

        head_sha = require_sha_primitive(
            head_sha,
            field="historical workflow head sha",
        )
        if (
            type(_pulls_per_page) is not int
            or _pulls_per_page <= 0
            or _pulls_per_page > 100
            or not callable(_encode_query)
            or getattr(_encode_query, "__code__", None) is not _encode_query_code
            or getattr(_encode_query, "__defaults__", None)
            is not _encode_query_defaults
            or getattr(_encode_query, "__kwdefaults__", None)
            is not _encode_query_kwdefaults
            or not request_dispatch_current()
        ):
            raise _error_type("historical association authority is unavailable")

        associated_numbers: set[int] = set()
        page = 1
        while True:
            query = _encode_query({"per_page": _pulls_per_page, "page": page})
            payload = _request_impl(self, f"/commits/{head_sha}/pulls?{query}")
            if not request_dispatch_current():
                raise _error_type("historical association request dispatch changed")
            if not isinstance(payload, list):
                raise _error_type(
                    "invalid historical commit pull-requests response"
                )
            for item in payload:
                if not isinstance(item, dict):
                    raise _error_type(
                        "invalid historical associated pull request"
                    )
                head = item.get("head")
                if not isinstance(head, dict):
                    raise _error_type(
                        "invalid historical associated pull request head"
                    )
                require_sha_primitive(
                    head.get("sha"),
                    field="historical associated pull request head",
                )
                associated_numbers.add(
                    require_positive_int_primitive(
                        item.get("number"),
                        field="historical associated pull request number",
                    )
                )
            if len(payload) < _pulls_per_page:
                break
            page += 1
        if not associated_numbers:
            raise _absent_type(
                "historical workflow head has no associated pull request"
            )
        if len(associated_numbers) != 1:
            raise _ambiguous_type(
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
        workflow_name = object.__getattribute__(
            self,
            "_WorkflowScopedGitHubApi__workflow_name",
        )
        if run.workflow_name == workflow_name:
            return run
        return WorkflowRun(
            run_id=run.run_id,
            head_sha=run.head_sha,
            workflow_name=workflow_name,
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

    def _historical_head_has_no_associated_prs(
        self,
        head_sha: str,
        *,
        _request_impl=GitHubApi._request,
        _request_code=GitHubApi._request.__code__,
        _request_defaults=GitHubApi._request.__defaults__,
        _request_kwdefaults=GitHubApi._request.__kwdefaults__,
        _request_kwdefault_items=tuple(GitHubApi._request.__kwdefaults__.items()),
        _pulls_per_page: int = _PULLS_PER_PAGE,
        _encode_query=urlencode,
        _encode_query_code=urlencode.__code__,
        _encode_query_defaults=urlencode.__defaults__,
        _encode_query_kwdefaults=urlencode.__kwdefaults__,
        _error_type=CancellationError,
    ) -> bool:
        """Return true only for a well-formed commit association response with zero PRs."""

        def request_dispatch_current() -> bool:
            bound = getattr(self, "_request", None)
            return (
                getattr(_request_impl, "__code__", None) is _request_code
                and getattr(_request_impl, "__defaults__", None)
                is _request_defaults
                and getattr(_request_impl, "__kwdefaults__", None)
                is _request_kwdefaults
                and len(_request_kwdefaults) == len(_request_kwdefault_items)
                and all(
                    key in _request_kwdefaults
                    and _request_kwdefaults[key] is value
                    for key, value in _request_kwdefault_items
                )
                and getattr(bound, "__self__", None) is self
                and getattr(bound, "__func__", None) is _request_impl
                and getattr(_encode_query, "__code__", None) is _encode_query_code
                and getattr(_encode_query, "__defaults__", None)
                is _encode_query_defaults
                and getattr(_encode_query, "__kwdefaults__", None)
                is _encode_query_kwdefaults
            )

        if type(head_sha) is not str or len(head_sha) != 40:
            raise _error_type("invalid historical workflow head sha")
        head_sha = head_sha.lower()
        if any(ch not in "0123456789abcdef" for ch in head_sha):
            raise _error_type("invalid historical workflow head sha")
        if (
            type(_pulls_per_page) is not int
            or _pulls_per_page <= 0
            or _pulls_per_page > 100
            or not callable(_encode_query)
            or getattr(_encode_query, "__code__", None) is not _encode_query_code
            or getattr(_encode_query, "__defaults__", None)
            is not _encode_query_defaults
            or getattr(_encode_query, "__kwdefaults__", None)
            is not _encode_query_kwdefaults
            or not request_dispatch_current()
        ):
            raise _error_type("historical association authority is unavailable")

        page = 1
        associated_numbers: set[int] = set()
        while True:
            query = _encode_query({"per_page": _pulls_per_page, "page": page})
            payload = _request_impl(self, f"/commits/{head_sha}/pulls?{query}")
            if not request_dispatch_current():
                raise _error_type("historical association request dispatch changed")
            if not isinstance(payload, list):
                raise _error_type(
                    "invalid historical commit pull-requests response"
                )
            for item in payload:
                if not isinstance(item, dict):
                    raise _error_type(
                        "invalid historical associated pull request"
                    )
                pr_number = item.get("number")
                if type(pr_number) is not int or pr_number <= 0:
                    raise _error_type(
                        "invalid historical associated pull request number"
                    )
                associated_numbers.add(pr_number)
            if len(payload) < _pulls_per_page:
                break
            page += 1
        return not associated_numbers


    def _canonical_branch_head(
        self,
        branch: str,
        *,
        _request_impl=GitHubApi._request,
        _request_code=GitHubApi._request.__code__,
        _encode_branch=quote,
        _encode_branch_code=quote.__code__,
        _encode_branch_defaults=quote.__defaults__,
        _encode_branch_kwdefaults=quote.__kwdefaults__,
        _allowed_http_error_type=_AllowedHttpError,
        _error_type=CancellationError,
    ) -> str | None:
        """Resolve one same-repository branch head; absence is authoritative."""

        def request_dispatch_current() -> bool:
            bound = getattr(self, "_request", None)
            return (
                getattr(_request_impl, "__code__", None) is _request_code
                and getattr(bound, "__self__", None) is self
                and getattr(bound, "__func__", None) is _request_impl
                and getattr(_encode_branch, "__code__", None) is _encode_branch_code
                and getattr(_encode_branch, "__defaults__", None)
                is _encode_branch_defaults
                and getattr(_encode_branch, "__kwdefaults__", None)
                is _encode_branch_kwdefaults
            )

        if (
            type(branch) is not str
            or not branch
            or not callable(_encode_branch)
            or getattr(_encode_branch, "__code__", None) is not _encode_branch_code
            or getattr(_encode_branch, "__defaults__", None)
            is not _encode_branch_defaults
            or getattr(_encode_branch, "__kwdefaults__", None)
            is not _encode_branch_kwdefaults
        ):
            raise _error_type("invalid canonical head branch")
        if not request_dispatch_current():
            raise _error_type("canonical branch request dispatch changed")
        encoded = _encode_branch(branch, safe="")
        payload = _request_impl(
            self,
            f"/git/ref/heads/{encoded}",
            allowed_http_errors=frozenset({404}),
        )
        if not request_dispatch_current():
            raise _error_type("canonical branch request dispatch changed")
        if type(payload) is _allowed_http_error_type:
            if payload.status_code == 404:
                return None
        if not isinstance(payload, dict):
            raise _error_type("invalid canonical branch response")
        target = payload.get("object")
        if not isinstance(target, dict):
            raise _error_type("invalid canonical branch target")
        target_sha = target.get("sha")
        if type(target_sha) is not str or len(target_sha) != 40:
            raise _error_type("invalid canonical branch head")
        target_sha = target_sha.lower()
        if any(ch not in "0123456789abcdef" for ch in target_sha):
            raise _error_type("invalid canonical branch head")
        return target_sha

    def _explicit_run_identity_matches(
        self,
        *,
        run_id: int,
        expected_head_sha: str,
        pr_number: int,
    ) -> bool:
        """Re-read one source run and require its explicit identity to remain exact.

        The workflow-wide scan is moving: GitHub can expose different pull-request
        metadata for the same run while statuses are enumerated.  A stable singleton
        observation therefore grants only provisional cancellation authority.  Before
        an explicit candidate reaches the irreversible cancel boundary, bind the exact
        run id back to this source workflow and require the same head and exact singleton
        PR reference.  Any transition to completed/unknown, empty/multi-reference,
        another PR, another head, another workflow or another event fails closed.
        """

        run_id = _require_positive_int(run_id, field="run id")
        expected_head_sha = _require_sha(
            expected_head_sha,
            field="expected workflow run head sha",
        )
        pr_number = _require_positive_int(pr_number, field="pull request number")
        workflow_id_authority = object.__getattribute__(
            self,
            "_WorkflowScopedGitHubApi__workflow_id",
        )
        payload = self._request(f"/actions/runs/{run_id}")
        if object.__getattribute__(
            self,
            "_WorkflowScopedGitHubApi__workflow_id",
        ) != workflow_id_authority:
            return False
        if not isinstance(payload, dict):
            raise CancellationError("invalid workflow-run response")
        workflow_id = _require_positive_int(
            payload.get("workflow_id"),
            field="workflow run workflow id",
        )
        if workflow_id != workflow_id_authority or payload.get("event") != "pull_request":
            return False
        run = parse_run(payload)
        return (
            run.run_id == run_id
            and run.head_sha == expected_head_sha
            and run.pr_numbers == (pr_number,)
        )

    def _build_cancel(
        base_cancel,
        request_impl,
        historical_associated_pr_number,
        historical_head_has_no_associated_prs,
        canonical_branch_head,
        live_pr_qualification,
        pull_request,
        qualification_state_reader,
    ):
        def freeze_default_metadata(function):
            positional = getattr(function, "__defaults__", None)
            keyword = getattr(function, "__kwdefaults__", None)
            keyword_items = tuple(keyword.items()) if keyword is not None else ()
            return positional, keyword, keyword_items

        def default_metadata_current(function, snapshot) -> bool:
            positional, keyword, keyword_items = snapshot
            if getattr(function, "__defaults__", None) is not positional:
                return False
            current_keyword = getattr(function, "__kwdefaults__", None)
            if current_keyword is not keyword:
                return False
            if keyword is None:
                return True
            if len(current_keyword) != len(keyword_items):
                return False
            return all(
                key in current_keyword and current_keyword[key] is value
                for key, value in keyword_items
            )

        base_cancel_code = getattr(base_cancel, "__code__", None)
        base_cancel_defaults = freeze_default_metadata(base_cancel)
        qualification_state_reader_code = getattr(
            qualification_state_reader,
            "__code__",
            None,
        )
        qualification_state_reader_defaults = freeze_default_metadata(
            qualification_state_reader
        )
        helper_dispatch = (
            (
                "_request",
                request_impl,
                getattr(request_impl, "__code__", None),
                freeze_default_metadata(request_impl),
            ),
            (
                "_historical_associated_pr_number",
                historical_associated_pr_number,
                getattr(historical_associated_pr_number, "__code__", None),
                freeze_default_metadata(historical_associated_pr_number),
            ),
            (
                "_historical_head_has_no_associated_prs",
                historical_head_has_no_associated_prs,
                getattr(historical_head_has_no_associated_prs, "__code__", None),
                freeze_default_metadata(historical_head_has_no_associated_prs),
            ),
            (
                "_canonical_branch_head",
                canonical_branch_head,
                getattr(canonical_branch_head, "__code__", None),
                freeze_default_metadata(canonical_branch_head),
            ),
            (
                "live_pr_qualification",
                live_pr_qualification,
                getattr(live_pr_qualification, "__code__", None),
                freeze_default_metadata(live_pr_qualification),
            ),
            (
                "_pull_request",
                pull_request,
                getattr(pull_request, "__code__", None),
                freeze_default_metadata(pull_request),
            ),
        )
        if (
            base_cancel_code is None
            or qualification_state_reader_code is None
            or any(
                implementation_code is None
                for _, _, implementation_code, _ in helper_dispatch
            )
        ):
            raise RuntimeError(
                "canonical scoped cancellation executable is unavailable"
            )

        def cancel(self, run_id: int) -> None:
            """Revalidate synthetic candidate identity at the irreversible boundary."""

            if (
                getattr(base_cancel, "__code__", None) is not base_cancel_code
                or not default_metadata_current(base_cancel, base_cancel_defaults)
                or getattr(qualification_state_reader, "__code__", None)
                is not qualification_state_reader_code
                or not default_metadata_current(
                    qualification_state_reader,
                    qualification_state_reader_defaults,
                )
            ):
                raise CancellationError(
                    "canonical base cancellation authority changed"
                )

            def require_helper_dispatch(name: str) -> None:
                match = None
                for (
                    helper_name,
                    expected,
                    expected_code,
                    expected_defaults,
                ) in helper_dispatch:
                    if helper_name != name:
                        continue
                    if match is not None:
                        raise CancellationError(
                            "scoped cancellation revalidation dispatch changed"
                        )
                    match = (expected, expected_code, expected_defaults)
                if match is None:
                    raise CancellationError(
                        "scoped cancellation revalidation dispatch changed"
                    )
                expected, expected_code, expected_defaults = match
                bound = getattr(self, name, None)
                if (
                    getattr(expected, "__code__", None) is not expected_code
                    or not default_metadata_current(expected, expected_defaults)
                    or getattr(bound, "__self__", None) is not self
                    or getattr(bound, "__func__", None) is not expected
                ):
                    raise CancellationError(
                        "scoped cancellation revalidation dispatch changed"
                    )

            for name, _, _, _ in helper_dispatch:
                require_helper_dispatch(name)

            # Keep the candidate key identical to the run selected by the controller.
            # A mutable module-global validator must not be able to redirect the lookup
            # away from recovered authority and then hand another run to base_cancel().
            if type(run_id) is not int or run_id <= 0:
                raise CancellationError("invalid run id")
            zero_association = self._zero_association_recovered_runs.get(run_id)
            recovered = self._recovered_runs.get(run_id)

            # Unbound/orphan recovery is seeded from a moving workflow-run collection.
            # Before any recovered identity can reach an irreversible POST, re-bind the
            # exact run id to this controller's canonical source workflow using the
            # captured canonical request transport. A forged/transient enumeration,
            # cross-workflow run id, completed run, changed head, or reappeared PR
            # reference therefore grants zero cancellation authority.
            recovered_candidate_head: str | None = None
            recovered_candidate_branch: str | None = None
            if zero_association is not None:
                recovered_candidate_head, recovered_candidate_branch = zero_association
            elif recovered is not None:
                recovered_candidate_head = recovered[1]
            if recovered_candidate_head is not None:
                workflow_id_authority = object.__getattribute__(
                    self,
                    "_WorkflowScopedGitHubApi__workflow_id",
                )
                repository_authority = object.__getattribute__(
                    self,
                    "_GitHubApi__repository",
                )
                require_helper_dispatch("_request")
                run_payload = request_impl(self, f"/actions/runs/{run_id}")
                require_helper_dispatch("_request")
                if (
                    object.__getattribute__(
                        self,
                        "_WorkflowScopedGitHubApi__workflow_id",
                    )
                    != workflow_id_authority
                    or object.__getattribute__(
                        self,
                        "_GitHubApi__repository",
                    )
                    != repository_authority
                ):
                    raise _CancellationAuthorityChanged(
                        "unbound workflow run identity changed"
                    )
                if not isinstance(run_payload, dict):
                    raise _CancellationAuthorityChanged(
                        "unbound workflow run identity changed"
                    )
                live_run_id = run_payload.get("id")
                live_workflow_id = run_payload.get("workflow_id")
                live_head_sha = run_payload.get("head_sha")
                live_status = run_payload.get("status")
                live_prs = run_payload.get("pull_requests")
                live_head_branch = run_payload.get("head_branch")
                live_head_repository = run_payload.get("head_repository")
                if (
                    type(live_run_id) is not int
                    or live_run_id != run_id
                    or type(live_workflow_id) is not int
                    or live_workflow_id != workflow_id_authority
                    or run_payload.get("event") != "pull_request"
                    or type(live_head_sha) is not str
                    or len(live_head_sha) != 40
                    or live_head_sha.lower() != recovered_candidate_head
                    or any(
                        ch not in "0123456789abcdef"
                        for ch in live_head_sha.lower()
                    )
                    or live_status
                    not in (
                        "queued",
                        "in_progress",
                        "waiting",
                        "pending",
                        "requested",
                    )
                    or type(live_prs) is not list
                    or live_prs
                    or (
                        recovered_candidate_branch is not None
                        and (
                            type(live_head_branch) is not str
                            or live_head_branch != recovered_candidate_branch
                            or type(live_head_repository) is not dict
                            or live_head_repository.get("full_name")
                            != repository_authority
                        )
                    )
                ):
                    raise _CancellationAuthorityChanged(
                        "unbound workflow run identity changed"
                    )

            if zero_association is not None:
                candidate_head_sha, head_branch = zero_association
                try:
                    require_helper_dispatch("_request")
                    require_helper_dispatch("_historical_head_has_no_associated_prs")
                    no_association = historical_head_has_no_associated_prs(
                        self, candidate_head_sha
                    )
                    require_helper_dispatch("_request")
                    require_helper_dispatch("_canonical_branch_head")
                    branch_head_sha = canonical_branch_head(self, head_branch)
                    require_helper_dispatch("_request")
                except CancellationError as exc:
                    raise CancellationError(
                        "unbound workflow run branch authority could not be revalidated"
                    ) from exc
                if not no_association or branch_head_sha == candidate_head_sha:
                    raise _CancellationAuthorityChanged(
                        "unbound workflow run branch authority changed"
                    )
    
            if recovered is not None:
                pr_number, candidate_head_sha = recovered
                try:
                    require_helper_dispatch("_request")
                    require_helper_dispatch("_historical_associated_pr_number")
                    associated_pr_number = historical_associated_pr_number(
                        self, candidate_head_sha
                    )
                    require_helper_dispatch("_request")
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
                require_helper_dispatch("_request")
                require_helper_dispatch("live_pr_qualification")
                require_helper_dispatch("_pull_request")
                qualification = live_pr_qualification(self, pr_number)
                require_helper_dispatch("_request")
                require_helper_dispatch("live_pr_qualification")
                require_helper_dispatch("_pull_request")
                if (
                    getattr(qualification_state_reader, "__code__", None)
                    is not qualification_state_reader_code
                ):
                    raise CancellationError(
                        "pull request qualification reader authority changed"
                    )
                qualification_head, integration_capable = (
                    qualification_state_reader(qualification)
                )
                if (
                    getattr(qualification_state_reader, "__code__", None)
                    is not qualification_state_reader_code
                ):
                    raise CancellationError(
                        "pull request qualification reader authority changed"
                    )
                if (
                    qualification_head == candidate_head_sha
                    and integration_capable
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
        GitHubApi._request,
        _historical_associated_pr_number,
        _historical_head_has_no_associated_prs,
        _canonical_branch_head,
        GitHubApi.live_pr_qualification,
        GitHubApi._pull_request,
        _pull_request_qualification_state,
    )
    del _build_cancel

    def _active_runs_for_status(
        self,
        status: str,
        *,
        _active_statuses: tuple[str, ...] = _ACTIVE_STATUSES,
        _runs_per_page: int = _RUNS_PER_PAGE,
        _encode_query=urlencode,
        _encode_query_code=urlencode.__code__,
        _encode_query_defaults=urlencode.__defaults__,
        _encode_query_kwdefaults=urlencode.__kwdefaults__,
        _run_parser=parse_run,
        _run_parser_code=parse_run.__code__,
        _run_parser_defaults=parse_run.__defaults__,
        _run_parser_kwdefaults=parse_run.__kwdefaults__,
        _run_parser_kwdefault_items=tuple(parse_run.__kwdefaults__.items())
        if parse_run.__kwdefaults__ is not None
        else (),
        _workflow_run_type=WorkflowRun,
    ) -> tuple[WorkflowRun, ...]:
        if (
            type(_active_statuses) is not tuple
            or not _active_statuses
            or any(type(item) is not str or not item for item in _active_statuses)
            or status not in _active_statuses
        ):
            raise CancellationError("invalid active workflow status")
        if (
            type(_runs_per_page) is not int
            or _runs_per_page <= 0
            or _runs_per_page > 100
            or not callable(_encode_query)
            or getattr(_encode_query, "__code__", None) is not _encode_query_code
            or getattr(_encode_query, "__defaults__", None)
            is not _encode_query_defaults
            or getattr(_encode_query, "__kwdefaults__", None)
            is not _encode_query_kwdefaults
            or getattr(_run_parser, "__code__", None) is not _run_parser_code
            or getattr(_run_parser, "__defaults__", None)
            is not _run_parser_defaults
            or getattr(_run_parser, "__kwdefaults__", None)
            is not _run_parser_kwdefaults
            or (
                _run_parser_kwdefaults is not None
                and (
                    len(_run_parser_kwdefaults)
                    != len(_run_parser_kwdefault_items)
                    or any(
                        key not in _run_parser_kwdefaults
                        or _run_parser_kwdefaults[key] is not value
                        for key, value in _run_parser_kwdefault_items
                    )
                )
            )
        ):
            raise CancellationError("active workflow pagination authority is unavailable")
        workflow_id = object.__getattribute__(
            self,
            "_WorkflowScopedGitHubApi__workflow_id",
        )
        workflow_name = object.__getattribute__(
            self,
            "_WorkflowScopedGitHubApi__workflow_name",
        )
        request_reader = self._request
        request_func = getattr(request_reader, "__func__", request_reader)
        request_code = getattr(request_func, "__code__", None)
        request_positional_defaults = getattr(request_func, "__defaults__", None)
        request_keyword_defaults = getattr(request_func, "__kwdefaults__", None)
        request_keyword_items = (
            tuple(request_keyword_defaults.items())
            if request_keyword_defaults is not None
            else ()
        )
        recovery_reader = self._recover_candidate_run_reference
        recovery_func = getattr(recovery_reader, "__func__", recovery_reader)
        recovery_code = getattr(recovery_func, "__code__", None)

        def snapshot_helpers_current() -> bool:
            bound_request = self._request
            bound_recovery = self._recover_candidate_run_reference
            return (
                request_code is not None
                and getattr(bound_request, "__func__", bound_request) is request_func
                and getattr(request_func, "__code__", None) is request_code
                and getattr(request_func, "__defaults__", None)
                is request_positional_defaults
                and getattr(request_func, "__kwdefaults__", None)
                is request_keyword_defaults
                and (
                    request_keyword_defaults is None
                    or (
                        len(request_keyword_defaults) == len(request_keyword_items)
                        and all(
                            key in request_keyword_defaults
                            and request_keyword_defaults[key] is value
                            for key, value in request_keyword_items
                        )
                    )
                )
                and getattr(_encode_query, "__code__", None) is _encode_query_code
                and getattr(_encode_query, "__defaults__", None)
                is _encode_query_defaults
                and getattr(_encode_query, "__kwdefaults__", None)
                is _encode_query_kwdefaults
                and getattr(_run_parser, "__code__", None) is _run_parser_code
                and getattr(_run_parser, "__defaults__", None)
                is _run_parser_defaults
                and getattr(_run_parser, "__kwdefaults__", None)
                is _run_parser_kwdefaults
                and (
                    _run_parser_kwdefaults is None
                    or (
                        len(_run_parser_kwdefaults)
                        == len(_run_parser_kwdefault_items)
                        and all(
                            key in _run_parser_kwdefaults
                            and _run_parser_kwdefaults[key] is value
                            for key, value in _run_parser_kwdefault_items
                        )
                    )
                )
                and recovery_code is not None
                and getattr(bound_recovery, "__func__", bound_recovery) is recovery_func
                and getattr(recovery_func, "__code__", None) is recovery_code
            )

        if not snapshot_helpers_current():
            raise CancellationError("active workflow snapshot authority changed")

        runs: list[WorkflowRun] = []
        # GitHub's total_count describes runs, not raw page observations. A moving
        # offset-paginated collection can overlap pages when newer runs arrive ahead of
        # the current offset. Track unique run ids separately so duplicate observations
        # cannot make the scan look complete and hide later active runs.
        seen_run_ids: set[int] = set()
        page = 1
        while True:
            query = _encode_query(
                {
                    "event": "pull_request",
                    "status": status,
                    "per_page": _runs_per_page,
                    "page": page,
                }
            )
            payload = request_reader(
                f"/actions/workflows/{workflow_id}/runs?{query}"
            )
            if not snapshot_helpers_current():
                raise CancellationError("active workflow snapshot authority changed")
            if (
                object.__getattribute__(
                    self,
                    "_WorkflowScopedGitHubApi__workflow_id",
                )
                != workflow_id
                or object.__getattribute__(
                    self,
                    "_WorkflowScopedGitHubApi__workflow_name",
                )
                != workflow_name
            ):
                raise CancellationError("source workflow binding changed")
            if (
                not isinstance(payload, dict)
                or type(payload.get("total_count")) is not int
                or payload["total_count"] < 0
                or not isinstance(payload.get("workflow_runs"), list)
            ):
                raise CancellationError("invalid workflow-runs response")
            page_runs = payload["workflow_runs"]
            if len(page_runs) > _runs_per_page:
                raise CancellationError("invalid workflow-runs page size")
            for item in page_runs:
                run = _run_parser(item)
                seen_run_ids.add(run.run_id)
                if run.workflow_name != workflow_name:
                    run = _workflow_run_type(
                        run_id=run.run_id,
                        head_sha=run.head_sha,
                        workflow_name=workflow_name,
                        pr_numbers=run.pr_numbers,
                        status=run.status,
                    )
                if not run.pr_numbers:
                    head_branch: str | None = None
                    if isinstance(item, dict):
                        raw_branch = item.get("head_branch")
                        head_repository = item.get("head_repository")
                        if (
                            isinstance(raw_branch, str)
                            and raw_branch
                            and isinstance(head_repository, dict)
                            and head_repository.get("full_name")
                            == object.__getattribute__(
                                self,
                                "_GitHubApi__repository",
                            )
                        ):
                            head_branch = raw_branch
                    candidate = (run.head_sha, head_branch)
                    previous = self._unbound_active_runs.get(run.run_id)
                    if (
                        run.run_id in self._explicit_active_run_ids
                        or run.run_id in self._conflicted_unbound_run_ids
                    ):
                        # Once this scan has observed any explicit PR metadata for the
                        # run, a later weaker unbound view cannot reopen orphan authority.
                        # Likewise, conflicting unbound observations remain deferred.
                        self._unbound_active_runs.pop(run.run_id, None)
                    elif previous is not None and previous != candidate:
                        # A run id is supposed to have immutable source identity. If a
                        # moving scan reports conflicting head/branch evidence while it
                        # is unbound, preserve neither observation as cancellation
                        # authority; a later stable controller sweep can reconsider it.
                        self._unbound_active_runs.pop(run.run_id, None)
                        self._conflicted_unbound_run_ids.add(run.run_id)
                    else:
                        self._unbound_active_runs[run.run_id] = candidate
                else:
                    # active_runs() queries statuses sequentially, so the same run can
                    # transition between requests. Any explicit PR observation is
                    # stronger than an unbound observation for that exact run id,
                    # regardless of observation order, and permanently revokes orphan
                    # cleanup for the remainder of this exact-workflow snapshot.
                    self._explicit_active_run_ids.add(run.run_id)
                    self._unbound_active_runs.pop(run.run_id, None)
                runs.append(recovery_reader(run))
            total_count = payload["total_count"]
            # Active-run collections are inherently moving while a controller scans
            # them. A short page is therefore a safe terminal snapshot even when the
            # earlier total_count was larger. Missing a concurrently transitioned run
            # can only defer cleanup; it cannot grant cancellation authority. Do not
            # turn harmless queue shrinkage into a failed controller invocation.
            if (
                not page_runs
                or len(page_runs) < _runs_per_page
                or len(seen_run_ids) >= total_count
            ):
                break
            page += 1
        return tuple(runs)

    def _build_active_runs(
        status_reader,
        recovery_reader,
        active_statuses,
        error_type,
    ):
        """Freeze the moving-snapshot reader graph outside mutable class/default metadata."""

        status_reader_code = getattr(status_reader, "__code__", None)
        recovery_reader_code = getattr(recovery_reader, "__code__", None)
        positional_defaults = getattr(status_reader, "__defaults__", None)
        keyword_defaults = getattr(status_reader, "__kwdefaults__", None)
        keyword_items = (
            tuple(keyword_defaults.items()) if keyword_defaults is not None else ()
        )
        if (
            status_reader_code is None
            or recovery_reader_code is None
            or type(active_statuses) is not tuple
            or not active_statuses
            or any(type(item) is not str or not item for item in active_statuses)
        ):
            raise RuntimeError("canonical active workflow snapshot authority is unavailable")

        def reader_graph_current(self) -> bool:
            bound_status_reader = getattr(self, "_active_runs_for_status", None)
            bound_recovery_reader = getattr(
                self,
                "_recover_candidate_run_reference",
                None,
            )
            if (
                getattr(status_reader, "__code__", None) is not status_reader_code
                or getattr(bound_status_reader, "__self__", None) is not self
                or getattr(bound_status_reader, "__func__", None) is not status_reader
                or getattr(recovery_reader, "__code__", None) is not recovery_reader_code
                or getattr(bound_recovery_reader, "__self__", None) is not self
                or getattr(bound_recovery_reader, "__func__", None) is not recovery_reader
                or getattr(status_reader, "__defaults__", None) is not positional_defaults
            ):
                return False
            current_keyword_defaults = getattr(
                status_reader,
                "__kwdefaults__",
                None,
            )
            if current_keyword_defaults is not keyword_defaults:
                return False
            if keyword_defaults is None:
                return True
            if len(current_keyword_defaults) != len(keyword_items):
                return False
            return all(
                key in current_keyword_defaults
                and current_keyword_defaults[key] is value
                for key, value in keyword_items
            )

        def active_runs(self) -> tuple[WorkflowRun, ...]:
            # Snapshot-local identity state must never leak across repeated scans on one API
            # object. A later invocation may observe a different stable queue and must derive
            # orphan authority only from that invocation's complete observation set.
            def reset_snapshot_authority() -> None:
                self._recovered_runs.clear()
                self._zero_association_recovered_runs.clear()
                self._unbound_active_runs.clear()
                self._explicit_active_run_ids.clear()
                self._conflicted_unbound_run_ids.clear()

            # Clear any prior generation before consulting mutable reader state. If this
            # generation fails after observing only some statuses, discard every partial
            # authority record before propagating the failure.
            reset_snapshot_authority()
            runs: list[WorkflowRun] = []
            try:
                if not reader_graph_current(self):
                    raise error_type("active workflow reader authority changed")
                for status in active_statuses:
                    if not reader_graph_current(self):
                        raise error_type("active workflow reader authority changed")
                    runs.extend(status_reader(self, status))
                    if not reader_graph_current(self):
                        raise error_type("active workflow reader authority changed")
            except Exception:
                reset_snapshot_authority()
                raise
            return tuple(runs)

        return active_runs

    active_runs = _build_active_runs(
        _active_runs_for_status,
        _recover_candidate_run_reference,
        _ACTIVE_STATUSES,
        CancellationError,
    )
    del _build_active_runs

    def cancel_historical_unbound_runs(
        self,
        *,
        exclude_run_ids: tuple[int, ...] = (),
        _cancel_effect=None,
        _cancel_effect_code=None,
        _qualification_reader=None,
        _qualification_reader_code=None,
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

        if _cancel_effect is None:
            _cancel_effect = _cancel_run_or_defer_active_conflict
            _cancel_effect_code = getattr(_cancel_effect, "__code__", None)
        if (
            _cancel_effect_code is None
            or getattr(_cancel_effect, "__code__", None) is not _cancel_effect_code
        ):
            raise CancellationError("canonical cancel effect authority changed")
        if _qualification_reader is None:
            _qualification_reader = _trusted_live_pr_qualification
            _qualification_reader_code = getattr(
                _qualification_reader,
                "__code__",
                None,
            )
        if (
            _qualification_reader_code is None
            or getattr(_qualification_reader, "__code__", None)
            is not _qualification_reader_code
        ):
            raise CancellationError("canonical qualification reader authority changed")

        def qualification_state_from_trusted_read(value) -> tuple[str, bool]:
            if type(value) is tuple:
                if len(value) != 2 or type(value[1]) is not bool:
                    raise CancellationError(
                        "invalid trusted pull request qualification"
                    )
                head_sha = value[0]
                if type(head_sha) is not str or len(head_sha) != 40:
                    raise CancellationError(
                        "invalid trusted pull request qualification"
                    )
                head_sha = head_sha.lower()
                if any(ch not in "0123456789abcdef" for ch in head_sha):
                    raise CancellationError(
                        "invalid trusted pull request qualification"
                    )
                return (head_sha, value[1])
            return _pull_request_qualification_state(value)

        if any(type(run_id) is not int or run_id <= 0 for run_id in exclude_run_ids):
            raise CancellationError("invalid excluded run id")
        excluded = set(exclude_run_ids)
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
                    if getattr(_cancel_effect, "__code__", None) is not _cancel_effect_code:
                        raise CancellationError("canonical cancel effect authority changed")
                    did_cancel = _cancel_effect(self, run_id)
                except _CancellationAuthorityChanged:
                    self._zero_association_recovered_runs.pop(run_id, None)
                    continue
                self._zero_association_recovered_runs.pop(run_id, None)
                if not did_cancel:
                    continue
                cancelled.append(run_id)
                continue
            except _HistoricalAssociationAmbiguous:
                continue
            except CancellationError:
                continue

            try:
                if (
                    getattr(_qualification_reader, "__code__", None)
                    is not _qualification_reader_code
                ):
                    raise CancellationError(
                        "canonical qualification reader authority changed"
                    )
                qualification = _qualification_reader(self, pr_number)
                if (
                    getattr(_qualification_reader, "__code__", None)
                    is not _qualification_reader_code
                ):
                    raise CancellationError(
                        "canonical qualification reader authority changed"
                    )
            except CancellationError:
                continue
            qualification_head, integration_capable = (
                qualification_state_from_trusted_read(qualification)
            )
            if (
                qualification_head == candidate_head_sha
                and integration_capable
            ):
                continue
            self._recovered_runs[run_id] = (pr_number, candidate_head_sha)
            try:
                if getattr(_cancel_effect, "__code__", None) is not _cancel_effect_code:
                    raise CancellationError("canonical cancel effect authority changed")
                did_cancel = _cancel_effect(self, run_id)
            except _CancellationAuthorityChanged:
                self._recovered_runs.pop(run_id, None)
                continue
            self._recovered_runs.pop(run_id, None)
            if not did_cancel:
                continue
            cancelled.append(run_id)
        return tuple(cancelled)


def _build_production_cancel_effect_boundary(
    fallback,
    production_api_type,
    production_cancel,
    error_type,
    active_conflict_message: str,
):
    """Seal production cancellation dispatch at the final irreversible boundary.

    Identity and live-qualification checks above prove an exact run coordinate. Do not
    hand that coordinate back to mutable instance/class cancel dispatch after those
    proofs. Production calls use the captured canonical scoped cancel executable;
    compatibility fakes keep the pre-existing fallback behavior.
    """

    production_cancel_code = getattr(production_cancel, "__code__", None)
    fallback_code = getattr(fallback, "__code__", None)
    if (
        production_cancel_code is None
        or fallback_code is None
        or type(active_conflict_message) is not str
        or not active_conflict_message
    ):
        raise RuntimeError("canonical cancellation effect boundary is unavailable")

    def cancel_or_defer(api: GitHubApi, run_id: int) -> bool:
        if type(api) is not production_api_type:
            if isinstance(api, production_api_type):
                raise error_type("workflow run cancellation API type changed")
            if getattr(fallback, "__code__", None) is not fallback_code:
                raise error_type("workflow run cancellation wrapper changed")
            return fallback(api, run_id)

        if type(run_id) is not int or run_id <= 0:
            raise error_type("invalid run id")
        bound_cancel = getattr(api, "cancel", None)
        if (
            getattr(production_cancel, "__code__", None)
            is not production_cancel_code
            or getattr(bound_cancel, "__self__", None) is not api
            or getattr(bound_cancel, "__func__", None) is not production_cancel
        ):
            raise error_type("workflow run cancellation dispatch changed")

        try:
            # Invoke the captured executable directly. A class or instance shadow can
            # no longer redirect the proven run id to another effect implementation.
            production_cancel(api, run_id)
        except error_type as exc:
            if (
                exc.__class__ is error_type
                and exc.args == (active_conflict_message,)
            ):
                return False
            raise
        return True

    return cancel_or_defer


_cancel_run_or_defer_active_conflict = _build_production_cancel_effect_boundary(
    _cancel_run_or_defer_active_conflict,
    WorkflowScopedGitHubApi,
    WorkflowScopedGitHubApi.cancel,
    CancellationError,
    "workflow run cancellation conflicted while run remains active",
)
del _build_production_cancel_effect_boundary


def _build_explicit_run_identity_checker(
    workflow_scoped_api_type,
    resolver,
    request_impl,
):
    def freeze_default_metadata(function):
        positional = getattr(function, "__defaults__", None)
        keyword = getattr(function, "__kwdefaults__", None)
        keyword_items = tuple(keyword.items()) if keyword is not None else ()
        return positional, keyword, keyword_items

    def default_metadata_current(function, snapshot) -> bool:
        positional, keyword, keyword_items = snapshot
        if getattr(function, "__defaults__", None) is not positional:
            return False
        current_keyword = getattr(function, "__kwdefaults__", None)
        if current_keyword is not keyword:
            return False
        if keyword is None:
            return True
        if len(current_keyword) != len(keyword_items):
            return False
        return all(
            key in current_keyword and current_keyword[key] is value
            for key, value in keyword_items
        )

    resolver_code = getattr(resolver, "__code__", None)
    request_code = getattr(request_impl, "__code__", None)
    request_defaults = freeze_default_metadata(request_impl)
    if resolver_code is None or request_code is None:
        raise RuntimeError("explicit run identity resolver executable is unavailable")

    def check(
        api: WorkflowScopedGitHubApi,
        *,
        run_id: int,
        expected_head_sha: str,
        pr_number: int,
    ) -> bool:
        """Fail closed when explicit identity or its transport dispatch changes."""

        if type(api) is workflow_scoped_api_type:
            workflow_id_authority = object.__getattribute__(
                api,
                "_WorkflowScopedGitHubApi__workflow_id",
            )

            def production_dispatch_current() -> bool:
                bound = getattr(api, "_explicit_run_identity_matches", None)
                bound_request = getattr(api, "_request", None)
                return (
                    getattr(resolver, "__code__", None) is resolver_code
                    and getattr(request_impl, "__code__", None) is request_code
                    and default_metadata_current(request_impl, request_defaults)
                    and getattr(bound, "__self__", None) is api
                    and getattr(bound, "__func__", None) is resolver
                    and getattr(bound_request, "__self__", None) is api
                    and getattr(bound_request, "__func__", None) is request_impl
                )

            if not production_dispatch_current():
                return False
            if type(run_id) is not int or run_id <= 0:
                return False
            if type(pr_number) is not int or pr_number <= 0:
                return False
            if type(expected_head_sha) is not str or len(expected_head_sha) != 40:
                return False
            expected_head_sha = expected_head_sha.lower()
            if any(ch not in "0123456789abcdef" for ch in expected_head_sha):
                return False
            try:
                # Call the captured canonical transport directly. This removes dynamic
                # self._request dispatch from the authority-producing GET itself; a
                # concurrent class/instance rebound therefore cannot shape the response.
                payload = request_impl(api, f"/actions/runs/{run_id}")
            except CancellationError:
                return False
            if not production_dispatch_current():
                return False
            if not isinstance(payload, dict):
                return False
            workflow_id = payload.get("workflow_id")
            if type(workflow_id) is not int or workflow_id <= 0:
                return False
            if object.__getattribute__(
                api,
                "_WorkflowScopedGitHubApi__workflow_id",
            ) != workflow_id_authority:
                return False
            if workflow_id != workflow_id_authority or payload.get("event") != "pull_request":
                return False
            if type(payload.get("id")) is not int or payload.get("id") != run_id:
                return False
            if payload.get("head_sha") != expected_head_sha:
                return False
            name = payload.get("name")
            status = payload.get("status")
            pulls = payload.get("pull_requests")
            if not isinstance(name, str) or not name or status not in _ACTIVE_STATUSES:
                return False
            if not isinstance(pulls, list) or len(pulls) != 1:
                return False
            pull = pulls[0]
            if not isinstance(pull, dict):
                return False
            return (
                type(pull.get("number")) is int
                and pull.get("number") == pr_number
            )

        if isinstance(api, workflow_scoped_api_type):
            return False
        candidate = getattr(api, "_explicit_run_identity_matches", None)
        if candidate is None:
            return True
        if not callable(candidate):
            return False
        try:
            return (
                candidate(
                    run_id=run_id,
                    expected_head_sha=expected_head_sha,
                    pr_number=pr_number,
                )
                is True
            )
        except CancellationError:
            return False

    return check


def _build_live_pr_qualification_reader(
    workflow_scoped_api_type,
    live_pr_qualification,
    pull_request,
    request_impl,
):
    def freeze_default_metadata(function):
        positional = getattr(function, "__defaults__", None)
        keyword = getattr(function, "__kwdefaults__", None)
        keyword_items = tuple(keyword.items()) if keyword is not None else ()
        return positional, keyword, keyword_items

    def default_metadata_current(function, snapshot) -> bool:
        positional, keyword, keyword_items = snapshot
        if getattr(function, "__defaults__", None) is not positional:
            return False
        current_keyword = getattr(function, "__kwdefaults__", None)
        if current_keyword is not keyword:
            return False
        if keyword is None:
            return True
        if len(current_keyword) != len(keyword_items):
            return False
        return all(
            key in current_keyword and current_keyword[key] is value
            for key, value in keyword_items
        )

    live_code = getattr(live_pr_qualification, "__code__", None)
    pull_code = getattr(pull_request, "__code__", None)
    request_code = getattr(request_impl, "__code__", None)
    request_defaults = freeze_default_metadata(request_impl)
    if live_code is None or pull_code is None or request_code is None:
        raise RuntimeError("live PR qualification executable is unavailable")

    def read(
        api: WorkflowScopedGitHubApi,
        pr_number: int,
    ) -> tuple[str, bool] | object:
        if type(api) is not workflow_scoped_api_type:
            if isinstance(api, workflow_scoped_api_type):
                raise CancellationError("live PR qualification API type changed")
            return api.live_pr_qualification(pr_number)

        def production_dispatch_current() -> bool:
            bound_live = getattr(api, "live_pr_qualification", None)
            bound_pull = getattr(api, "_pull_request", None)
            bound_request = getattr(api, "_request", None)
            return (
                getattr(live_pr_qualification, "__code__", None) is live_code
                and getattr(pull_request, "__code__", None) is pull_code
                and getattr(request_impl, "__code__", None) is request_code
                and default_metadata_current(request_impl, request_defaults)
                and getattr(bound_live, "__self__", None) is api
                and getattr(bound_live, "__func__", None) is live_pr_qualification
                and getattr(bound_pull, "__self__", None) is api
                and getattr(bound_pull, "__func__", None) is pull_request
                and getattr(bound_request, "__self__", None) is api
                and getattr(bound_request, "__func__", None) is request_impl
            )

        if not production_dispatch_current():
            raise CancellationError("live PR qualification dispatch changed")
        if type(pr_number) is not int or pr_number <= 0:
            raise CancellationError("invalid pull request number")
        repository = object.__getattribute__(
            api,
            "_GitHubApi__repository",
        )
        # This read is cancellation authority. Avoid the nested dynamic
        # live_pr_qualification -> self._pull_request -> self._request path:
        # a transient nested shadow could restore canonical dispatch before the outer
        # pre/post witness observes it. Use the captured canonical transport directly.
        payload = request_impl(api, f"/pulls/{pr_number}")
        if not production_dispatch_current():
            raise CancellationError("live PR qualification dispatch changed")
        if (
            object.__getattribute__(api, "_GitHubApi__repository")
            != repository
        ):
            raise CancellationError("GitHub API repository binding changed")
        if not isinstance(payload, dict):
            raise CancellationError("invalid pull request response")
        head = payload.get("head")
        base = payload.get("base")
        state = payload.get("state")
        draft = payload.get("draft")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise CancellationError("invalid pull request head/base")
        if state not in ("open", "closed") or type(draft) is not bool:
            raise CancellationError("invalid pull request qualification state")
        base_repo = base.get("repo")
        if (
            not isinstance(base_repo, dict)
            or base_repo.get("full_name") != repository
        ):
            raise CancellationError("pull request base repository is not canonical")
        head_repo = head.get("repo")
        same_repository_head = (
            isinstance(head_repo, dict)
            and head_repo.get("full_name") == repository
        )
        head_sha = head.get("sha")
        if type(head_sha) is not str or len(head_sha) != 40:
            raise CancellationError("invalid live pull request head")
        head_sha = head_sha.lower()
        if any(ch not in "0123456789abcdef" for ch in head_sha):
            raise CancellationError("invalid live pull request head")
        return (
            head_sha,
            state == "open"
            and draft is False
            and same_repository_head,
        )

    return read

_explicit_run_identity_is_current = _build_explicit_run_identity_checker(
    WorkflowScopedGitHubApi,
    WorkflowScopedGitHubApi._explicit_run_identity_matches,
    GitHubApi._request,
)
_trusted_live_pr_qualification = _build_live_pr_qualification_reader(
    WorkflowScopedGitHubApi,
    GitHubApi.live_pr_qualification,
    GitHubApi._pull_request,
    GitHubApi._request,
)
del _build_explicit_run_identity_checker
del _build_live_pr_qualification_reader


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

    if type(current_run_id) is not int or current_run_id <= 0:
        raise CancellationError("invalid current run id")
    if type(event_head_sha) is not str or len(event_head_sha) != 40:
        raise CancellationError("invalid event head sha")
    event_head_sha = event_head_sha.lower()
    if any(ch not in "0123456789abcdef" for ch in event_head_sha):
        raise CancellationError("invalid event head sha")
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
    snapshot_pr_number = next(iter(pr_numbers))
    if type(snapshot_pr_number) is not int or snapshot_pr_number <= 0:
        raise CancellationError("invalid snapshot pull request number")
    return snapshot_pr_number


def _validated_event_pr_identity(
    pr_number: int,
    *,
    reference_mode: str,
) -> tuple[int | None, bool]:
    """Preserve event-level empty vs multi-reference identity without collapsing them.

    A workflow_run event that carried multiple PR references is permanently ambiguous
    for cancellation of that exact triggering source run. A later Actions snapshot may
    be used to recover a singleton only when the event carried no PR reference at all;
    it must never erase positive evidence that the event was multi-reference.
    """

    if type(pr_number) is not int or pr_number < 0:
        raise CancellationError("pull request number cannot be negative")
    if reference_mode == "singleton":
        if pr_number <= 0:
            raise CancellationError("invalid pull request number")
        return (pr_number, False)
    if reference_mode not in ("empty", "ambiguous"):
        raise CancellationError("invalid event pull request reference mode")
    if pr_number != 0:
        raise CancellationError(
            "non-singleton event pull request mode requires zero pull request number"
        )
    return None, reference_mode == "ambiguous"


def cancel_superseded_explicit_pr_runs(
    api: WorkflowScopedGitHubApi,
    *,
    workflow_name: str,
    current_run_id: int,
    runs: tuple[WorkflowRun, ...] | None = None,
    _selector=select_superseded_runs,
    _selector_code=select_superseded_runs.__code__,
    _cancel_effect=_cancel_run_or_defer_active_conflict,
    _cancel_effect_code=_cancel_run_or_defer_active_conflict.__code__,
    _qualification_reader=None,
    _qualification_reader_code=None,
    _identity_checker=None,
    _identity_checker_code=None,
    _production_api_type=WorkflowScopedGitHubApi,
    _production_qualification_reader=_trusted_live_pr_qualification,
    _production_qualification_reader_code=_trusted_live_pr_qualification.__code__,
    _production_identity_checker=_explicit_run_identity_is_current,
    _production_identity_checker_code=_explicit_run_identity_is_current.__code__,
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

    if getattr(_cancel_effect, "__code__", None) is not _cancel_effect_code:
        raise CancellationError("canonical cancel effect authority changed")
    if type(api) is _production_api_type:
        _qualification_reader = _production_qualification_reader
        _qualification_reader_code = _production_qualification_reader_code
        _identity_checker = _production_identity_checker
        _identity_checker_code = _production_identity_checker_code
    elif isinstance(api, _production_api_type):
        raise CancellationError("decision authority API type changed")
    else:
        # Compatibility fixtures may inject their own authority readers before entry.
        # Production never derives these effect-authorizing helpers from mutable globals.
        if _qualification_reader is None:
            _qualification_reader = _trusted_live_pr_qualification
            _qualification_reader_code = getattr(_qualification_reader, "__code__", None)
        if _identity_checker is None:
            _identity_checker = _explicit_run_identity_is_current
            _identity_checker_code = getattr(_identity_checker, "__code__", None)

    def require_decision_authorities() -> None:
        if (
            getattr(_selector, "__code__", None) is not _selector_code
            or _qualification_reader_code is None
            or getattr(_qualification_reader, "__code__", None)
            is not _qualification_reader_code
            or _identity_checker_code is None
            or getattr(_identity_checker, "__code__", None) is not _identity_checker_code
        ):
            raise CancellationError("canonical decision authority changed")

    require_decision_authorities()

    def qualification_state_from_trusted_read(value) -> tuple[str, bool]:
        # The production trusted reader already returns a primitive canonical tuple.
        # Consume that tuple directly so a later module-global fixture adapter rebind
        # cannot rewrite READY/non-READY authority after the external read.
        if type(value) is tuple:
            if len(value) != 2 or type(value[1]) is not bool:
                raise CancellationError("invalid trusted pull request qualification")
            head_sha = value[0]
            if type(head_sha) is not str or len(head_sha) != 40:
                raise CancellationError("invalid trusted pull request qualification")
            head_sha = head_sha.lower()
            if any(ch not in "0123456789abcdef" for ch in head_sha):
                raise CancellationError("invalid trusted pull request qualification")
            return (head_sha, value[1])
        return _pull_request_qualification_state(value)

    if type(current_run_id) is not int or current_run_id <= 0:
        raise CancellationError("invalid current run id")
    if (
        type(workflow_name) is not str
        or not workflow_name
        or workflow_name != object.__getattribute__(
            api,
            "_WorkflowScopedGitHubApi__workflow_name",
        )
    ):
        raise CancellationError("workflow name does not match exact workflow id")

    if runs is None:
        runs = api.active_runs()
    elif type(runs) is not tuple or any(
        not isinstance(run, WorkflowRun) for run in runs
    ):
        raise CancellationError("invalid exact-workflow active-run snapshot")
    observations_by_run_id: dict[int, list[WorkflowRun]] = {}
    for run in runs:
        if run.workflow_name == workflow_name:
            observations_by_run_id.setdefault(run.run_id, []).append(run)

    # One moving Actions scan can observe the same run id more than once as its
    # status/metadata changes between requests. Never let an older singleton view
    # authorize cancellation when any observation of that exact run disagrees on
    # head or PR identity. A later controller can clean it once identity is stable.
    stable_singletons: list[WorkflowRun] = []
    for observations in observations_by_run_id.values():
        first = observations[0]
        if len(first.pr_numbers) != 1:
            continue
        if any(
            observation.head_sha != first.head_sha
            or observation.pr_numbers != first.pr_numbers
            for observation in observations[1:]
        ):
            continue
        stable_singletons.append(first)
    explicit_singleton_runs = tuple(stable_singletons)
    explicit_singletons_by_id = {
        run.run_id: run for run in explicit_singleton_runs
    }
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
            require_decision_authorities()
            qualification = _qualification_reader(api, pr_number)
            require_decision_authorities()
        except CancellationError:
            # Qualification authority is scoped to one PR group. Failure to resolve
            # one group must fail that group closed without starving independent PRs
            # whose own live authority can still be proven.
            continue
        qualification_state = qualification_state_from_trusted_read(qualification)
        qualification_head, integration_capable = qualification_state
        selected = _selector(
            explicit_singleton_runs,
            pr_number=pr_number,
            live_head_sha=qualification_head,
            workflow_name=workflow_name,
            current_run_id=current_run_id,
            cancel_same_head=not integration_capable,
        )
        # The selector is a convenience projection, not an irreversible-effect
        # authority. Revalidate every returned id against this function's already
        # frozen snapshot and live qualification before any run-identity reread or
        # cancellation. A rebound selector may omit work, but it cannot widen the
        # cancellation set, redirect another PR group, or select the triggering run.
        if type(selected) is not tuple:
            continue
        validated_selected: list[int] = []
        selection_seen: set[int] = set()
        selection_valid = True
        for run_id in selected:
            if (
                type(run_id) is not int
                or run_id <= 0
                or run_id == current_run_id
                or run_id in selection_seen
            ):
                selection_valid = False
                break
            candidate = explicit_singletons_by_id.get(run_id)
            if (
                candidate is None
                or candidate.workflow_name != workflow_name
                or candidate.pr_numbers != (pr_number,)
                or (
                    integration_capable
                    and candidate.head_sha == qualification_head
                )
            ):
                selection_valid = False
                break
            selection_seen.add(run_id)
            validated_selected.append(run_id)
        if not selection_valid:
            continue
        for run_id in validated_selected:
            if run_id in cancelled_ids:
                continue
            candidate = explicit_singletons_by_id[run_id]
            require_decision_authorities()
            if not _identity_checker(
                api,
                run_id=run_id,
                expected_head_sha=candidate.head_sha,
                pr_number=pr_number,
            ):
                require_decision_authorities()
                continue
            require_decision_authorities()
            # The run-identity reread above may itself take a network round trip. Keep
            # live PR head/lifecycle qualification as the final external authority
            # check before the irreversible cancellation.
            try:
                require_decision_authorities()
                current_qualification = _qualification_reader(api, pr_number)
                require_decision_authorities()
            except CancellationError:
                break
            if (
                qualification_state_from_trusted_read(current_qualification)
                != qualification_state
            ):
                break
            if getattr(_cancel_effect, "__code__", None) is not _cancel_effect_code:
                raise CancellationError("canonical cancel effect authority changed")
            if not _cancel_effect(api, run_id):
                continue
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
    _cancel_effect=_cancel_run_or_defer_active_conflict,
    _cancel_effect_code=_cancel_run_or_defer_active_conflict.__code__,
    _qualification_reader=None,
    _qualification_reader_code=None,
    _identity_checker=None,
    _identity_checker_code=None,
    _production_api_type=WorkflowScopedGitHubApi,
    _production_qualification_reader=_trusted_live_pr_qualification,
    _production_qualification_reader_code=_trusted_live_pr_qualification.__code__,
    _production_identity_checker=_explicit_run_identity_is_current,
    _production_identity_checker_code=_explicit_run_identity_is_current.__code__,
) -> bool:
    """Cancel a source run proven stale or same-head non-integration-capable.

    Controller concurrency may intentionally coalesce multiple source-head events for
    the same explicit PR + workflow.  Therefore the surviving controller must remain
    useful even when its triggering source run is stale.  The irreversible POST is
    allowed only after a fresh live qualification snapshot still matches the snapshot
    used for the decision.
    """

    if getattr(_cancel_effect, "__code__", None) is not _cancel_effect_code:
        raise CancellationError("canonical cancel effect authority changed")
    if type(api) is _production_api_type:
        _qualification_reader = _production_qualification_reader
        _qualification_reader_code = _production_qualification_reader_code
        _identity_checker = _production_identity_checker
        _identity_checker_code = _production_identity_checker_code
    elif isinstance(api, _production_api_type):
        raise CancellationError("decision authority API type changed")
    else:
        if _qualification_reader is None:
            _qualification_reader = _trusted_live_pr_qualification
            _qualification_reader_code = getattr(_qualification_reader, "__code__", None)
        if _identity_checker is None:
            _identity_checker = _explicit_run_identity_is_current
            _identity_checker_code = getattr(_identity_checker, "__code__", None)

    def require_decision_authorities() -> None:
        if (
            _qualification_reader_code is None
            or getattr(_qualification_reader, "__code__", None)
            is not _qualification_reader_code
            or _identity_checker_code is None
            or getattr(_identity_checker, "__code__", None) is not _identity_checker_code
        ):
            raise CancellationError("canonical decision authority changed")

    require_decision_authorities()

    def qualification_state_from_trusted_read(value) -> tuple[str, bool]:
        if type(value) is tuple:
            if len(value) != 2 or type(value[1]) is not bool:
                raise CancellationError("invalid trusted pull request qualification")
            head_sha = value[0]
            if type(head_sha) is not str or len(head_sha) != 40:
                raise CancellationError("invalid trusted pull request qualification")
            head_sha = head_sha.lower()
            if any(ch not in "0123456789abcdef" for ch in head_sha):
                raise CancellationError("invalid trusted pull request qualification")
            return (head_sha, value[1])
        return _pull_request_qualification_state(value)

    if type(event_head_sha) is not str or len(event_head_sha) != 40:
        raise CancellationError("invalid event head sha")
    event_head_sha = event_head_sha.lower()
    if any(ch not in "0123456789abcdef" for ch in event_head_sha):
        raise CancellationError("invalid event head sha")
    if type(current_run_id) is not int or current_run_id <= 0:
        raise CancellationError("invalid current run id")
    qualification_state = qualification_state_from_trusted_read(qualification)
    qualification_head, integration_capable = qualification_state
    stale = qualification_head != event_head_sha
    same_head_nonqualifying = (
        qualification_head == event_head_sha
        and not integration_capable
    )
    if not stale and not same_head_nonqualifying:
        return False
    require_decision_authorities()
    if not _identity_checker(
        api,
        run_id=current_run_id,
        expected_head_sha=event_head_sha,
        pr_number=pr_number,
    ):
        require_decision_authorities()
        return False
    require_decision_authorities()
    try:
        current_qualification = _qualification_reader(api, pr_number)
        require_decision_authorities()
    except CancellationError:
        require_decision_authorities()
        # A failed authority reread grants no trigger cancellation authority, but it
        # must not invalidate independently completed workflow-wide cleanup.
        return False
    if (
        qualification_state_from_trusted_read(current_qualification)
        != qualification_state
    ):
        return False
    if getattr(_cancel_effect, "__code__", None) is not _cancel_effect_code:
        raise CancellationError("canonical cancel effect authority changed")
    return _cancel_effect(api, current_run_id)


def _build_main(
    *,
    module_globals,
    api_type,
    base_api_type,
    sweep_impl,
    trigger_impl,
    snapshot_identity_impl,
    trusted_qualification_impl,
    event_identity_impl,
    orphan_effect_impl,
):
    """Freeze scoped-controller orchestration roots outside caller metadata."""

    def freeze_default_metadata(function):
        positional = getattr(function, "__defaults__", None)
        keyword = getattr(function, "__kwdefaults__", None)
        keyword_items = tuple(keyword.items()) if keyword is not None else ()
        return positional, keyword, keyword_items

    def default_metadata_current(function, snapshot) -> bool:
        positional, keyword, keyword_items = snapshot
        if getattr(function, "__defaults__", None) is not positional:
            return False
        current_keyword = getattr(function, "__kwdefaults__", None)
        if current_keyword is not keyword:
            return False
        if keyword is None:
            return True
        if len(current_keyword) != len(keyword_items):
            return False
        return all(
            key in current_keyword and current_keyword[key] is value
            for key, value in keyword_items
        )

    api_init = api_type.__dict__.get("__init__")
    api_init_code = getattr(api_init, "__code__", None)
    base_request_impl = base_api_type.__dict__.get("_request")
    base_request_code = getattr(base_request_impl, "__code__", None)
    base_cancel_impl = base_api_type.__dict__.get("cancel")
    base_cancel_code = getattr(base_cancel_impl, "__code__", None)
    scoped_cancel_impl = api_type.__dict__.get("cancel")
    scoped_cancel_code = getattr(scoped_cancel_impl, "__code__", None)
    base_request_globals = getattr(base_request_impl, "__globals__", {})
    request_type = base_request_globals.get("Request")
    request_init = request_type.__dict__.get("__init__")
    request_init_code = getattr(request_init, "__code__", None)
    urlopen_impl = base_request_globals.get("urlopen")
    urlopen_code = getattr(urlopen_impl, "__code__", None)
    argparse_module = module_globals.get("argparse")
    parser_type = argparse_module.ArgumentParser
    parser_init = parser_type.__dict__.get("__init__")
    parser_init_code = getattr(parser_init, "__code__", None)
    parser_add_argument = getattr(parser_type, "add_argument", None)
    parser_add_argument_code = getattr(parser_add_argument, "__code__", None)
    parser_parse_args = getattr(parser_type, "parse_args", None)
    parser_parse_args_code = getattr(parser_parse_args, "__code__", None)
    os_module = module_globals.get("os")
    environment = os_module.environ
    active_runs_impl = api_type.__dict__.get("active_runs")
    active_runs_code = getattr(active_runs_impl, "__code__", None)
    orphan_impl = api_type.__dict__.get("cancel_historical_unbound_runs")
    orphan_code = getattr(orphan_impl, "__code__", None)
    sweep_code = getattr(sweep_impl, "__code__", None)
    trigger_code = getattr(trigger_impl, "__code__", None)
    snapshot_identity_code = getattr(snapshot_identity_impl, "__code__", None)
    trusted_qualification_code = getattr(trusted_qualification_impl, "__code__", None)
    event_identity_code = getattr(event_identity_impl, "__code__", None)
    orphan_effect_code = getattr(orphan_effect_impl, "__code__", None)

    api_init_defaults = freeze_default_metadata(api_init)
    base_request_defaults = freeze_default_metadata(base_request_impl)
    base_cancel_defaults = freeze_default_metadata(base_cancel_impl)
    scoped_cancel_defaults = freeze_default_metadata(scoped_cancel_impl)
    parser_init_defaults = freeze_default_metadata(parser_init)
    parser_add_argument_defaults = freeze_default_metadata(parser_add_argument)
    parser_parse_args_defaults = freeze_default_metadata(parser_parse_args)
    active_runs_defaults = freeze_default_metadata(active_runs_impl)
    orphan_defaults = freeze_default_metadata(orphan_impl)
    sweep_defaults = freeze_default_metadata(sweep_impl)
    trigger_defaults = freeze_default_metadata(trigger_impl)
    snapshot_identity_defaults = freeze_default_metadata(snapshot_identity_impl)
    trusted_qualification_defaults = freeze_default_metadata(trusted_qualification_impl)
    event_identity_defaults = freeze_default_metadata(event_identity_impl)
    orphan_effect_defaults = freeze_default_metadata(orphan_effect_impl)

    def main(argv: list[str] | None) -> int:
        # Production dispatch roots are closure-owned from module composition time.
        # Callers cannot rebase the comparison graph through main() arguments/defaults.
        def orchestration_authority_current() -> bool:
            return (
                module_globals.get("WorkflowScopedGitHubApi") is api_type
                and api_type.__dict__.get("__init__") is api_init
                and getattr(api_init, "__code__", None) is api_init_code
                and default_metadata_current(api_init, api_init_defaults)
                and module_globals.get("GitHubApi") is base_api_type
                and base_api_type.__dict__.get("_request") is base_request_impl
                and getattr(base_request_impl, "__code__", None) is base_request_code
                and default_metadata_current(
                    base_request_impl, base_request_defaults
                )
                and base_api_type.__dict__.get("cancel") is base_cancel_impl
                and getattr(base_cancel_impl, "__code__", None) is base_cancel_code
                and default_metadata_current(
                    base_cancel_impl, base_cancel_defaults
                )
                and getattr(api_type, "_request", None) is base_request_impl
                and api_type.__dict__.get("cancel") is scoped_cancel_impl
                and getattr(scoped_cancel_impl, "__code__", None)
                is scoped_cancel_code
                and default_metadata_current(
                    scoped_cancel_impl, scoped_cancel_defaults
                )
                and getattr(api_type, "cancel", None) is scoped_cancel_impl
                and base_request_globals.get("Request") is request_type
                and request_type.__dict__.get("__init__") is request_init
                and getattr(request_init, "__code__", None) is request_init_code
                and base_request_globals.get("urlopen") is urlopen_impl
                and getattr(urlopen_impl, "__code__", None) is urlopen_code
                and module_globals.get("argparse") is argparse_module
                and argparse_module.ArgumentParser is parser_type
                and parser_type.__dict__.get("__init__") is parser_init
                and getattr(parser_init, "__code__", None) is parser_init_code
                and default_metadata_current(parser_init, parser_init_defaults)
                and getattr(parser_type, "add_argument", None)
                is parser_add_argument
                and getattr(parser_add_argument, "__code__", None)
                is parser_add_argument_code
                and default_metadata_current(
                    parser_add_argument, parser_add_argument_defaults
                )
                and getattr(parser_type, "parse_args", None) is parser_parse_args
                and getattr(parser_parse_args, "__code__", None)
                is parser_parse_args_code
                and default_metadata_current(
                    parser_parse_args, parser_parse_args_defaults
                )
                and module_globals.get("os") is os_module
                and os_module.environ is environment
                and api_type.__dict__.get("active_runs") is active_runs_impl
                and getattr(active_runs_impl, "__code__", None) is active_runs_code
                and default_metadata_current(active_runs_impl, active_runs_defaults)
                and (
                    api_type.__dict__.get("cancel_historical_unbound_runs")
                    is orphan_impl
                )
                and getattr(orphan_impl, "__code__", None) is orphan_code
                and default_metadata_current(orphan_impl, orphan_defaults)
                and (
                    module_globals.get("cancel_superseded_explicit_pr_runs")
                    is sweep_impl
                )
                and getattr(sweep_impl, "__code__", None) is sweep_code
                and default_metadata_current(sweep_impl, sweep_defaults)
                and (
                    module_globals.get(
                        "_cancel_triggering_run_if_stale_or_nonqualifying"
                    )
                    is trigger_impl
                )
                and getattr(trigger_impl, "__code__", None) is trigger_code
                and default_metadata_current(trigger_impl, trigger_defaults)
                and (
                    module_globals.get("_explicit_singleton_pr_for_current_run")
                    is snapshot_identity_impl
                )
                and (
                    getattr(snapshot_identity_impl, "__code__", None)
                    is snapshot_identity_code
                )
                and default_metadata_current(
                    snapshot_identity_impl, snapshot_identity_defaults
                )
                and (
                    module_globals.get("_trusted_live_pr_qualification")
                    is trusted_qualification_impl
                )
                and (
                    getattr(trusted_qualification_impl, "__code__", None)
                    is trusted_qualification_code
                )
                and default_metadata_current(
                    trusted_qualification_impl, trusted_qualification_defaults
                )
                and (
                    module_globals.get("_validated_event_pr_identity")
                    is event_identity_impl
                )
                and getattr(event_identity_impl, "__code__", None) is event_identity_code
                and default_metadata_current(
                    event_identity_impl, event_identity_defaults
                )
                and (
                    module_globals.get("_cancel_run_or_defer_active_conflict")
                    is orphan_effect_impl
                )
                and getattr(orphan_effect_impl, "__code__", None)
                is orphan_effect_code
                and default_metadata_current(
                    orphan_effect_impl, orphan_effect_defaults
                )
            )

        def require_main_dispatch() -> None:
            if not orchestration_authority_current():
                raise CancellationError("controller orchestration authority changed")

        try:
            require_main_dispatch()

            parser = parser_type()
            parser_add_argument(parser, "--pr-number", type=int, required=True)
            parser_add_argument(
                parser,
                "--event-pr-reference-mode",
                choices=("empty", "singleton", "ambiguous"),
                required=True,
            )
            parser_add_argument(parser, "--event-head-sha", required=True)
            parser_add_argument(parser, "--workflow-name", required=True)
            parser_add_argument(parser, "--workflow-id", type=int, required=True)
            parser_add_argument(parser, "--current-run-id", type=int, required=True)
            args = parser_parse_args(parser, argv)

            require_main_dispatch()
            api = api_type(
                repository=environment.get("GITHUB_REPOSITORY", ""),
                token=environment.get("GITHUB_TOKEN", ""),
                workflow_id=args.workflow_id,
                workflow_name=args.workflow_name,
            )
            require_main_dispatch()

            event_head_sha = args.event_head_sha
            if type(event_head_sha) is not str or len(event_head_sha) != 40:
                raise CancellationError("invalid event head sha")
            event_head_sha = event_head_sha.lower()
            if any(ch not in "0123456789abcdef" for ch in event_head_sha):
                raise CancellationError("invalid event head sha")
            if type(args.current_run_id) is not int or args.current_run_id <= 0:
                raise CancellationError("invalid current run id")
            current_run_id = args.current_run_id

            require_main_dispatch()
            trigger_pr_number, event_identity_ambiguous = event_identity_impl(
                args.pr_number,
                reference_mode=args.event_pr_reference_mode,
            )
            require_main_dispatch()

            # The triggering event's explicit singleton PR is retained only for the
            # separately boundary-revalidated triggering-source-run decision after the
            # workflow-wide sweep. Empty and multi-reference event identity are deliberately
            # distinct: an empty event may recover one same-run/same-head singleton from the
            # exact Actions snapshot, while a multi-reference event permanently withholds
            # single-PR cancellation authority for that triggering source run. Neither case
            # blocks cleanup for independent explicit PR groups in the source workflow.

            # One exact-workflow snapshot now reconciles every explicit singleton PR group.
            # If the trigger event itself carried no unambiguous PR identity, retain this
            # same snapshot long enough to recover only a consistent explicit singleton
            # identity for the current source run. This closes the event/snapshot metadata
            # race without granting identity from scheduler state or historical heuristics.
            sweep_runs: tuple[WorkflowRun, ...] | None = None
            snapshot_trigger_pr_number: int | None = None
            if trigger_pr_number is None and not event_identity_ambiguous:
                require_main_dispatch()
                sweep_runs = active_runs_impl(api)
                require_main_dispatch()
                snapshot_trigger_pr_number = snapshot_identity_impl(
                    sweep_runs,
                    workflow_name=args.workflow_name,
                    current_run_id=current_run_id,
                    event_head_sha=event_head_sha,
                )
                require_main_dispatch()

            require_main_dispatch()
            sweep_cancelled = sweep_impl(
                api,
                workflow_name=args.workflow_name,
                current_run_id=current_run_id,
                runs=sweep_runs,
            )
            require_main_dispatch()

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
                if trigger_pr_number is not None or event_identity_ambiguous
                else sweep_cancelled
            )

            require_main_dispatch()
            orphan_cancelled = orphan_impl(
                api,
                exclude_run_ids=orphan_excluded_run_ids,
                _cancel_effect=orphan_effect_impl,
                _cancel_effect_code=orphan_effect_code,
                _qualification_reader=trusted_qualification_impl,
                _qualification_reader_code=trusted_qualification_code,
            )
            require_main_dispatch()

            if trigger_pr_number is not None:
                # Refresh after the potentially long sweep; the helper itself rereads once
                # more immediately before cancelling this exact triggering source run.
                try:
                    require_main_dispatch()
                    trigger_qualification = trusted_qualification_impl(
                        api, trigger_pr_number
                    )
                    require_main_dispatch()
                except CancellationError:
                    # Trigger qualification is authority for this one source run only.
                    # Failure proves no cancellation authority and must not turn already
                    # completed workflow-wide reconciliation into a controller failure.
                    trigger_qualification = None
                if trigger_qualification is not None:
                    require_main_dispatch()
                    trigger_impl(
                        api,
                        pr_number=trigger_pr_number,
                        event_head_sha=event_head_sha,
                        current_run_id=current_run_id,
                        qualification=trigger_qualification,
                    )
                    require_main_dispatch()
        except CancellationError as exc:
            print(f"superseded-run cancellation failed: {exc}", file=sys.stderr)
            return 2
        cancelled = ",".join(str(item) for item in sweep_cancelled)
        orphaned = ",".join(str(item) for item in orphan_cancelled)
        print("cancelled superseded workflow runs: " + cancelled)
        print("cancelled historical unbound workflow runs: " + orphaned)
        return 0


    return main


main = _build_main(
    module_globals=globals(),
    api_type=WorkflowScopedGitHubApi,
    base_api_type=GitHubApi,
    sweep_impl=cancel_superseded_explicit_pr_runs,
    trigger_impl=_cancel_triggering_run_if_stale_or_nonqualifying,
    snapshot_identity_impl=_explicit_singleton_pr_for_current_run,
    trusted_qualification_impl=_trusted_live_pr_qualification,
    event_identity_impl=_validated_event_pr_identity,
    orphan_effect_impl=_cancel_run_or_defer_active_conflict,
)
del _build_main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
