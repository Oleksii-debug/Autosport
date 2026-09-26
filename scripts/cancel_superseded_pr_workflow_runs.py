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


class CancellationError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkflowRun:
    run_id: int
    head_sha: str
    workflow_name: str
    pr_numbers: tuple[int, ...]
    status: str


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
    if status not in {"queued", "in_progress", "waiting", "pending", "requested"}:
        raise CancellationError("invalid active workflow status")
    if not isinstance(pulls, list):
        raise CancellationError("invalid pull_requests")
    pr_numbers: list[int] = []
    for item in pulls:
        if not isinstance(item, dict):
            raise CancellationError("invalid pull request reference")
        pr_numbers.append(_require_positive_int(item.get("number"), field="pull request number"))
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
        if run.run_id != current_run_id
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

    def _request(self, path: str, *, method: str = "GET") -> object:
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
        except (HTTPError, URLError, TimeoutError) as exc:
            raise CancellationError(f"GitHub API request failed: {type(exc).__name__}") from exc
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

    def active_runs(self) -> tuple[WorkflowRun, ...]:
        runs: list[WorkflowRun] = []
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            query = urlencode({"event": "pull_request", "status": status, "per_page": 100})
            payload = self._request(f"/actions/runs?{query}")
            if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
                raise CancellationError("invalid workflow-runs response")
            runs.extend(parse_run(item) for item in payload["workflow_runs"])
        return tuple(runs)

    def cancel(self, run_id: int) -> None:
        run_id = _require_positive_int(run_id, field="run id")
        payload = self._request(f"/actions/runs/{run_id}/cancel", method="POST")
        if payload is not None:
            raise CancellationError("unexpected cancel response body")


def cancel_superseded(
    *,
    api: GitHubApi,
    pr_number: int,
    event_head_sha: str,
    workflow_name: str,
    current_run_id: int,
) -> tuple[int, ...]:
    event_head_sha = _require_sha(event_head_sha, field="event head sha")
    live_head_sha = api.live_pr_head(pr_number)
    if event_head_sha != live_head_sha:
        return ()
    active_runs = api.active_runs()
    if api.live_pr_head(pr_number) != live_head_sha:
        return ()
    selected = select_superseded_runs(
        active_runs,
        pr_number=pr_number,
        live_head_sha=live_head_sha,
        workflow_name=workflow_name,
        current_run_id=current_run_id,
    )
    for run_id in selected:
        api.cancel(run_id)
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--event-head-sha", required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--current-run-id", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        api = GitHubApi(
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            token=os.environ.get("GITHUB_TOKEN", ""),
        )
        cancelled = cancel_superseded(
            api=api,
            pr_number=args.pr_number,
            event_head_sha=args.event_head_sha,
            workflow_name=args.workflow_name,
            current_run_id=args.current_run_id,
        )
    except CancellationError as exc:
        print(f"superseded-run cancellation failed: {exc}", file=sys.stderr)
        return 2
    print("cancelled superseded workflow runs: " + ",".join(str(item) for item in cancelled))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
