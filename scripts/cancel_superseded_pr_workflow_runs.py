from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_API_VERSION = "2022-11-28"
_ACCEPT = "application/vnd.github+json"
_ACTIVE_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")
_RUNS_PER_PAGE = 100


class CancellationError(RuntimeError):
    pass


@dataclass(frozen=True)
class _AllowedHttpError:
    status_code: int


@dataclass(frozen=True)
class WorkflowRun:
    run_id: int
    head_sha: str
    workflow_name: str
    pr_numbers: tuple[int, ...]
    status: str


@dataclass(frozen=True)
class CancellationResult:
    current_head: bool
    cancelled_run_ids: tuple[int, ...]


def _require_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 40:
        raise CancellationError(f"invalid {field}")
    lowered = value.lower()
    if any(ch not in "0123456789abcdef" for ch in lowered):
        raise CancellationError(f"invalid {field}")
    return lowered


def _require_positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise CancellationError(f"invalid {field}")
    return value


def parse_run(payload: object) -> WorkflowRun:
    if not isinstance(payload, dict):
        raise CancellationError("workflow run must be an object")
    run_id = _require_positive_int(payload.get("id"), field="run id")
    head_sha = _require_sha(payload.get("head_sha"), field="run head_sha")
    name = payload.get("name")
    status = payload.get("status")
    pulls = payload.get("pull_requests")
    if not isinstance(name, str) or not name:
        raise CancellationError("invalid workflow name")
    if status not in _ACTIVE_STATUSES:
        raise CancellationError("invalid active workflow status")
    if not isinstance(pulls, list):
        raise CancellationError("invalid pull_requests")
    pr_numbers: list[int] = []
    for item in pulls:
        if not isinstance(item, dict):
            raise CancellationError("invalid pull request reference")
        pr_numbers.append(
            _require_positive_int(item.get("number"), field="pull request number")
        )
    return WorkflowRun(
        run_id=run_id,
        head_sha=head_sha,
        workflow_name=name,
        pr_numbers=tuple(pr_numbers),
        status=status,
    )


def select_superseded_runs(
    runs: Iterable[WorkflowRun],
    *,
    pr_number: int,
    live_head_sha: str,
    workflow_name: str,
    current_run_id: int,
) -> tuple[int, ...]:
    pr_number = _require_positive_int(pr_number, field="pull request number")
    current_run_id = _require_positive_int(current_run_id, field="current run id")
    live_head_sha = _require_sha(live_head_sha, field="live head sha")
    if not workflow_name:
        raise CancellationError("workflow name is required")
    selected = {
        run.run_id
        for run in runs
        if run.run_id < current_run_id
        and run.workflow_name == workflow_name
        and pr_number in run.pr_numbers
        and run.head_sha != live_head_sha
    }
    return tuple(sorted(selected))


class GitHubApi:
    def __init__(self, *, repository: str, token: str) -> None:
        parts = repository.split("/")
        if len(parts) != 2 or not all(parts):
            raise CancellationError("GITHUB_REPOSITORY must be owner/repo")
        if not token:
            raise CancellationError("GITHUB_TOKEN is required")
        self._repository = repository
        self._token = token

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        request = Request(
            f"https://api.github.com/repos/{self._repository}{path}",
            method=method,
            headers={
                "Accept": _ACCEPT,
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": _API_VERSION,
                "User-Agent": "autosport-ci-superseded-run-canceller",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read()
        except HTTPError as exc:
            if exc.code in allowed_http_errors:
                return _AllowedHttpError(exc.code)
            raise CancellationError(
                f"GitHub API request failed: {type(exc).__name__}"
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise CancellationError(
                f"GitHub API request failed: {type(exc).__name__}"
            ) from exc
        if not body:
            return None
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CancellationError("GitHub API returned invalid JSON") from exc

    def live_pr_head(self, pr_number: int) -> str:
        payload = self._request(f"/pulls/{pr_number}")
        if not isinstance(payload, dict):
            raise CancellationError("invalid pull request response")
        head = payload.get("head")
        if not isinstance(head, dict):
            raise CancellationError("invalid pull request head")
        return _require_sha(head.get("sha"), field="live pull request head")

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
            payload = self._request(f"/actions/runs?{query}")
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
            if not page_runs or len(runs) >= total_count:
                break
            if len(page_runs) < _RUNS_PER_PAGE:
                raise CancellationError(
                    "workflow-runs pagination ended before reported total_count"
                )
            page += 1
        return tuple(runs)

    def active_runs(self) -> tuple[WorkflowRun, ...]:
        runs: list[WorkflowRun] = []
        for status in _ACTIVE_STATUSES:
            runs.extend(self._active_runs_for_status(status))
        return tuple(runs)

    def workflow_run_status(self, run_id: int) -> str:
        run_id = _require_positive_int(run_id, field="run id")
        payload = self._request(f"/actions/runs/{run_id}")
        if not isinstance(payload, dict):
            raise CancellationError("invalid workflow-run response")
        status = payload.get("status")
        if status != "completed" and status not in _ACTIVE_STATUSES:
            raise CancellationError("invalid workflow-run status")
        return status

    def cancel(self, run_id: int) -> None:
        run_id = _require_positive_int(run_id, field="run id")
        payload = self._request(
            f"/actions/runs/{run_id}/cancel",
            method="POST",
            allowed_http_errors=frozenset({409}),
        )
        if isinstance(payload, _AllowedHttpError):
            if payload.status_code != 409:
                raise CancellationError("unexpected allowed cancellation HTTP status")
            if self.workflow_run_status(run_id) == "completed":
                return
            raise CancellationError(
                "workflow run cancellation conflicted while run remains active"
            )
        if payload is not None:
            raise CancellationError("unexpected cancel response body")


def admit_current_head(
    *,
    api: GitHubApi,
    pr_number: int,
    event_head_sha: str,
) -> CancellationResult:
    """Read-only PR admission: only the current live head may allocate heavy work."""

    event_head_sha = _require_sha(event_head_sha, field="event head sha")
    live_head_sha = api.live_pr_head(pr_number)
    return CancellationResult(
        current_head=event_head_sha == live_head_sha,
        cancelled_run_ids=(),
    )


def cancel_superseded(
    *,
    api: GitHubApi,
    pr_number: int,
    event_head_sha: str,
    workflow_name: str,
    current_run_id: int,
) -> CancellationResult:
    event_head_sha = _require_sha(event_head_sha, field="event head sha")
    live_head_sha = api.live_pr_head(pr_number)
    if event_head_sha != live_head_sha:
        return CancellationResult(current_head=False, cancelled_run_ids=())
    active_runs = api.active_runs()
    if api.live_pr_head(pr_number) != live_head_sha:
        return CancellationResult(current_head=False, cancelled_run_ids=())
    selected = select_superseded_runs(
        active_runs,
        pr_number=pr_number,
        live_head_sha=live_head_sha,
        workflow_name=workflow_name,
        current_run_id=current_run_id,
    )
    cancelled: list[int] = []
    for run_id in selected:
        # The PR can move again, including an ABA reset to an older SHA, after the
        # post-listing check. Re-resolve immediately before every irreversible POST.
        if api.live_pr_head(pr_number) != live_head_sha:
            return CancellationResult(
                current_head=False,
                cancelled_run_ids=tuple(cancelled),
            )
        api.cancel(run_id)
        cancelled.append(run_id)
    return CancellationResult(current_head=True, cancelled_run_ids=tuple(cancelled))


def _write_github_output(result: CancellationResult) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    try:
        with open(output_path, "a", encoding="utf-8") as output:
            value = "true" if result.current_head else "false"
            output.write(f"current_head={value}\n")
    except OSError as exc:
        raise CancellationError("unable to write GITHUB_OUTPUT") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--event-head-sha", required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--current-run-id", type=int, required=True)
    parser.add_argument("--admission-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        api = GitHubApi(
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            token=os.environ.get("GITHUB_TOKEN", ""),
        )
        if args.admission_only:
            result = admit_current_head(
                api=api,
                pr_number=args.pr_number,
                event_head_sha=args.event_head_sha,
            )
        else:
            result = cancel_superseded(
                api=api,
                pr_number=args.pr_number,
                event_head_sha=args.event_head_sha,
                workflow_name=args.workflow_name,
                current_run_id=args.current_run_id,
            )
        _write_github_output(result)
    except CancellationError as exc:
        print(f"superseded-run cancellation failed: {exc}", file=sys.stderr)
        return 2
    cancelled = ",".join(str(item) for item in result.cancelled_run_ids)
    print("cancelled superseded workflow runs: " + cancelled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
