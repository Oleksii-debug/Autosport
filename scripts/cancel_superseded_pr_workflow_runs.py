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
_PULLS_PER_PAGE = 100


class CancellationError(RuntimeError):
    pass


@dataclass(frozen=True)
class _AllowedHttpError:
    status_code: int


@dataclass(frozen=True)
class _CancellationAccepted:
    pass


_CANCELLATION_ACCEPTED = _CancellationAccepted()


@dataclass(frozen=True)
class WorkflowRun:
    run_id: int
    head_sha: str
    workflow_name: str
    pr_numbers: tuple[int, ...]
    status: str


@dataclass(frozen=True)
class PullRequestQualification:
    head_sha: str
    integration_capable: bool


def _build_pull_request_qualification_state_reader(
    qualification_type,
    qualification_dict_descriptor,
    qualification_init,
):
    """Freeze the qualification DTO trust root at module composition time."""

    qualification_init_code = getattr(qualification_init, "__code__", None)
    if qualification_init_code is None:
        raise RuntimeError("pull request qualification constructor is unavailable")
    field_class_witnesses = tuple(
        (
            name,
            name in qualification_type.__dict__,
            qualification_type.__dict__.get(name),
        )
        for name in ("head_sha", "integration_capable")
    )

    def read(qualification: object) -> tuple[str, bool]:
        """Normalize exact primitive production state or the frozen fixture DTO."""

        if type(qualification) is tuple:
            if len(qualification) != 2:
                raise CancellationError("invalid pull request qualification state")
            head_sha, integration_capable = qualification
            if type(head_sha) is not str or len(head_sha) != 40:
                raise CancellationError("invalid live pull request head")
            head_sha = head_sha.lower()
            if any(ch not in "0123456789abcdef" for ch in head_sha):
                raise CancellationError("invalid live pull request head")
            if type(integration_capable) is not bool:
                raise CancellationError("invalid pull request integration capability")
            return head_sha, integration_capable

        if (
            qualification_type.__dict__.get("__dict__")
            is not qualification_dict_descriptor
            or qualification_type.__dict__.get("__init__") is not qualification_init
            or getattr(qualification_init, "__code__", None)
            is not qualification_init_code
            or type(qualification) is not qualification_type
        ):
            raise CancellationError("pull request qualification authority changed")
        for name, expected_present, expected_value in field_class_witnesses:
            if (
                (name in qualification_type.__dict__) is not expected_present
                or qualification_type.__dict__.get(name) is not expected_value
            ):
                raise CancellationError("pull request qualification authority changed")

        state = qualification_dict_descriptor.__get__(
            qualification,
            qualification_type,
        )
        if type(state) is not dict or set(state) != {
            "head_sha",
            "integration_capable",
        }:
            raise CancellationError("invalid pull request qualification state")
        head_sha = dict.__getitem__(state, "head_sha")
        integration_capable = dict.__getitem__(state, "integration_capable")
        if type(head_sha) is not str or len(head_sha) != 40:
            raise CancellationError("invalid live pull request head")
        head_sha = head_sha.lower()
        if any(ch not in "0123456789abcdef" for ch in head_sha):
            raise CancellationError("invalid live pull request head")
        if type(integration_capable) is not bool:
            raise CancellationError("invalid pull request integration capability")
        return head_sha, integration_capable

    return read


_pull_request_qualification_state = _build_pull_request_qualification_state_reader(
    PullRequestQualification,
    PullRequestQualification.__dict__["__dict__"],
    PullRequestQualification.__dict__["__init__"],
)
del _build_pull_request_qualification_state_reader


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


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise CancellationError("GitHub API JSON contains duplicate object key")
        value[key] = item
    return value


def _reject_nonstandard_json_constant(value: str) -> object:
    raise CancellationError(
        f"GitHub API JSON contains non-standard constant: {value}"
    )


def parse_run(
    payload: object,
    *,
    _positive_int=_require_positive_int,
    _positive_int_code=_require_positive_int.__code__,
    _sha_validator=_require_sha,
    _sha_validator_code=_require_sha.__code__,
    _active_statuses: tuple[str, ...] = _ACTIVE_STATUSES,
    _workflow_run_type=WorkflowRun,
    _workflow_run_dict_descriptor=WorkflowRun.__dict__["__dict__"],
    _workflow_run_init=WorkflowRun.__dict__["__init__"],
    _workflow_run_init_code=WorkflowRun.__dict__["__init__"].__code__,
) -> WorkflowRun:
    if (
        getattr(_positive_int, "__code__", None) is not _positive_int_code
        or getattr(_sha_validator, "__code__", None) is not _sha_validator_code
        or type(_active_statuses) is not tuple
        or not _active_statuses
        or _workflow_run_type.__dict__.get("__dict__")
        is not _workflow_run_dict_descriptor
        or _workflow_run_type.__dict__.get("__init__") is not _workflow_run_init
        or getattr(_workflow_run_init, "__code__", None)
        is not _workflow_run_init_code
    ):
        raise CancellationError("workflow run parser authority changed")
    if not isinstance(payload, dict):
        raise CancellationError("workflow run must be an object")
    run_id = _positive_int(payload.get("id"), field="run id")
    head_sha = _sha_validator(payload.get("head_sha"), field="run head_sha")
    name = payload.get("name")
    status = payload.get("status")
    pulls = payload.get("pull_requests")
    if not isinstance(name, str) or not name:
        raise CancellationError("invalid workflow name")
    if status not in _active_statuses:
        raise CancellationError("invalid active workflow status")
    if not isinstance(pulls, list):
        raise CancellationError("invalid pull_requests")
    pr_numbers: list[int] = []
    for item in pulls:
        if not isinstance(item, dict):
            raise CancellationError("invalid pull request reference")
        pr_numbers.append(
            _positive_int(item.get("number"), field="pull request number")
        )
    run = _workflow_run_type(
        run_id=run_id,
        head_sha=head_sha,
        workflow_name=name,
        pr_numbers=tuple(pr_numbers),
        status=status,
    )
    if (
        _workflow_run_type.__dict__.get("__dict__")
        is not _workflow_run_dict_descriptor
        or _workflow_run_type.__dict__.get("__init__") is not _workflow_run_init
        or getattr(_workflow_run_init, "__code__", None)
        is not _workflow_run_init_code
        or type(run) is not _workflow_run_type
    ):
        raise CancellationError("workflow run parser authority changed")
    state = _workflow_run_dict_descriptor.__get__(run, _workflow_run_type)
    if type(state) is not dict or set(state) != {
        "run_id",
        "head_sha",
        "workflow_name",
        "pr_numbers",
        "status",
    }:
        raise CancellationError("workflow run parser authority changed")
    if (
        dict.__getitem__(state, "run_id") != run_id
        or dict.__getitem__(state, "head_sha") != head_sha
        or dict.__getitem__(state, "workflow_name") != name
        or dict.__getitem__(state, "pr_numbers") != tuple(pr_numbers)
        or dict.__getitem__(state, "status") != status
    ):
        raise CancellationError("workflow run parser authority changed")
    return run


def select_superseded_runs(
    runs: Iterable[WorkflowRun],
    *,
    pr_number: int,
    live_head_sha: str,
    workflow_name: str,
    current_run_id: int,
    cancel_same_head: bool = False,
) -> tuple[int, ...]:
    pr_number = _require_positive_int(pr_number, field="pull request number")
    current_run_id = _require_positive_int(current_run_id, field="current run id")
    live_head_sha = _require_sha(live_head_sha, field="live head sha")
    if not workflow_name:
        raise CancellationError("workflow name is required")
    if type(cancel_same_head) is not bool:
        raise CancellationError("cancel_same_head must be boolean")
    selected = {
        run.run_id
        for run in runs
        if run.run_id != current_run_id
        and run.workflow_name == workflow_name
        and run.pr_numbers == (pr_number,)
        and (cancel_same_head or run.head_sha != live_head_sha)
    }
    return tuple(sorted(selected))


class GitHubApi:
    def __init__(self, *, repository: str, token: str) -> None:
        if type(repository) is not str:
            raise CancellationError("GITHUB_REPOSITORY must be owner/repo")
        parts = repository.split("/")
        if len(parts) != 2 or not all(parts):
            raise CancellationError("GITHUB_REPOSITORY must be owner/repo")
        if not token:
            raise CancellationError("GITHUB_TOKEN is required")
        self.__repository = repository
        self._token = token

    @property
    def _repository(self) -> str:
        return object.__getattribute__(self, "_GitHubApi__repository")

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
        _json_loads=json.loads,
        _json_loads_code=json.loads.__code__,
        _json_loads_globals=json.loads.__globals__,
        _json_decoder_type=json.JSONDecoder,
        _json_decoder_init=json.JSONDecoder.__dict__["__init__"],
        _json_decoder_init_code=json.JSONDecoder.__dict__["__init__"].__code__,
        _json_decoder_decode=json.JSONDecoder.__dict__["decode"],
        _json_decoder_decode_code=json.JSONDecoder.__dict__["decode"].__code__,
        _json_decoder_raw_decode=json.JSONDecoder.__dict__["raw_decode"],
        _json_decoder_raw_decode_code=json.JSONDecoder.__dict__["raw_decode"].__code__,
        _json_decoder_globals=json.JSONDecoder.__dict__["__init__"].__globals__,
        _json_decoder_scanner=json.JSONDecoder.__dict__["__init__"].__globals__["scanner"],
        _json_decoder_make_scanner=json.JSONDecoder.__dict__["__init__"].__globals__["scanner"].make_scanner,
        _json_decoder_object=json.JSONDecoder.__dict__["__init__"].__globals__["JSONObject"],
        _json_decoder_object_code=json.JSONDecoder.__dict__["__init__"].__globals__["JSONObject"].__code__,
        _json_decoder_array=json.JSONDecoder.__dict__["__init__"].__globals__["JSONArray"],
        _json_decoder_array_code=json.JSONDecoder.__dict__["__init__"].__globals__["JSONArray"].__code__,
        _json_decoder_scanstring=json.JSONDecoder.__dict__["__init__"].__globals__["scanstring"],
        _json_parse_int=int,
        _json_parse_float=float,
        _json_decode_error=json.JSONDecodeError,
        _strict_object_hook=_strict_json_object,
        _strict_object_hook_code=_strict_json_object.__code__,
        _reject_constant_hook=_reject_nonstandard_json_constant,
        _reject_constant_hook_code=_reject_nonstandard_json_constant.__code__,
    ) -> object:
        repository = object.__getattribute__(self, "_GitHubApi__repository")
        is_cancel_request = (
            method == "POST"
            and path.startswith("/actions/runs/")
            and path.endswith("/cancel")
        )
        request = Request(
            f"https://api.github.com/repos/{repository}{path}",
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
                status_code = response.status
                if is_cancel_request:
                    if type(status_code) is not int or status_code != 202:
                        raise CancellationError(
                            "workflow run cancellation returned unexpected HTTP status"
                        )
                    # For this endpoint the documented HTTP 202 Accepted status is the
                    # success authority. The response body is non-authoritative and is
                    # deliberately neither read nor parsed. The closure-built cancel()
                    # additionally seals this exact request implementation by identity.
                    return _CANCELLATION_ACCEPTED
                if type(status_code) is not int or status_code != 200:
                    raise CancellationError(
                        "GitHub API GET returned unexpected HTTP status"
                    )
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

        def json_parser_authority_current() -> bool:
            return (
                getattr(_json_loads, "__code__", None) is _json_loads_code
                and _json_loads_globals.get("JSONDecoder") is _json_decoder_type
                and _json_decoder_type.__dict__.get("__init__") is _json_decoder_init
                and getattr(_json_decoder_init, "__code__", None)
                is _json_decoder_init_code
                and _json_decoder_type.__dict__.get("decode") is _json_decoder_decode
                and getattr(_json_decoder_decode, "__code__", None)
                is _json_decoder_decode_code
                and _json_decoder_type.__dict__.get("raw_decode")
                is _json_decoder_raw_decode
                and getattr(_json_decoder_raw_decode, "__code__", None)
                is _json_decoder_raw_decode_code
                and _json_decoder_globals.get("scanner") is _json_decoder_scanner
                and getattr(_json_decoder_scanner, "make_scanner", None)
                is _json_decoder_make_scanner
                and _json_decoder_globals.get("JSONObject") is _json_decoder_object
                and getattr(_json_decoder_object, "__code__", None)
                is _json_decoder_object_code
                and _json_decoder_globals.get("JSONArray") is _json_decoder_array
                and getattr(_json_decoder_array, "__code__", None)
                is _json_decoder_array_code
                and _json_decoder_globals.get("scanstring") is _json_decoder_scanstring
                and getattr(_strict_object_hook, "__code__", None)
                is _strict_object_hook_code
                and getattr(_reject_constant_hook, "__code__", None)
                is _reject_constant_hook_code
            )

        if not json_parser_authority_current():
            raise CancellationError("GitHub API JSON parser authority changed")
        try:
            payload = _json_loads(
                body,
                cls=_json_decoder_type,
                object_pairs_hook=_strict_object_hook,
                parse_int=_json_parse_int,
                parse_float=_json_parse_float,
                parse_constant=_reject_constant_hook,
            )
        except (UnicodeDecodeError, _json_decode_error) as exc:
            if not json_parser_authority_current():
                raise CancellationError(
                    "GitHub API JSON parser authority changed"
                ) from exc
            raise CancellationError("GitHub API returned invalid JSON") from exc
        if not json_parser_authority_current():
            raise CancellationError("GitHub API JSON parser authority changed")
        return payload

    def _pull_request(self, pr_number: int) -> dict[str, object]:
        if type(pr_number) is not int or pr_number <= 0:
            raise CancellationError("invalid pull request number")
        payload = self._request(f"/pulls/{pr_number}")
        if not isinstance(payload, dict):
            raise CancellationError("invalid pull request response")
        return payload

    def associated_pr_number(
        self,
        head_sha: str,
        *,
        _pulls_per_page: int = _PULLS_PER_PAGE,
        _encode_query=urlencode,
        _encode_query_code=urlencode.__code__,
        _sha_validator=_require_sha,
        _sha_validator_code=_require_sha.__code__,
        _positive_int=_require_positive_int,
        _positive_int_code=_require_positive_int.__code__,
    ) -> int:
        """Resolve a missing workflow_run PR reference from its exact source head.

        GitHub may omit workflow_run.pull_requests for close/merge lifecycle runs. The
        commit association endpoint is trusted API data, but head equality alone is not
        enough to identify the emitting PR: another PR may have used the same commit and
        later advanced. Cancellation authority is therefore granted only when the commit
        is associated with exactly one PR in total and that same PR still names the exact
        event head. Historical cross-PR reuse, zero matches, and ambiguity fail closed.

        The provider page bound, query encoder and primitive validators are frozen at
        composition. The bound request executable is captured before the first external
        read so a callback cannot redirect a later association page.
        """

        request_impl = self._request
        request_func = getattr(request_impl, "__func__", request_impl)
        request_code = getattr(request_func, "__code__", None)

        def request_dispatch_current() -> bool:
            bound = self._request
            bound_func = getattr(bound, "__func__", bound)
            return (
                request_code is not None
                and bound_func is request_func
                and getattr(request_func, "__code__", None) is request_code
                and getattr(_encode_query, "__code__", None) is _encode_query_code
                and getattr(_sha_validator, "__code__", None) is _sha_validator_code
                and getattr(_positive_int, "__code__", None) is _positive_int_code
            )

        if (
            type(_pulls_per_page) is not int
            or _pulls_per_page <= 0
            or _pulls_per_page > 100
            or not callable(_encode_query)
            or getattr(_encode_query, "__code__", None) is not _encode_query_code
            or not request_dispatch_current()
        ):
            raise CancellationError("commit association authority is unavailable")

        head_sha = _sha_validator(head_sha, field="event head sha")
        associated_numbers: set[int] = set()
        exact_numbers: set[int] = set()
        page = 1
        while True:
            query = _encode_query({"per_page": _pulls_per_page, "page": page})
            payload = request_impl(f"/commits/{head_sha}/pulls?{query}")
            if not request_dispatch_current():
                raise CancellationError("commit association request dispatch changed")
            if not isinstance(payload, list):
                raise CancellationError("invalid commit pull-requests response")
            for item in payload:
                if not isinstance(item, dict):
                    raise CancellationError("invalid associated pull request")
                head = item.get("head")
                if not isinstance(head, dict):
                    raise CancellationError("invalid associated pull request head")
                candidate_sha = _sha_validator(
                    head.get("sha"), field="associated pull request head"
                )
                candidate_number = _positive_int(
                    item.get("number"), field="associated pull request number"
                )
                associated_numbers.add(candidate_number)
                if candidate_sha == head_sha:
                    exact_numbers.add(candidate_number)
            if len(payload) < _pulls_per_page:
                break
            page += 1
        if (
            len(associated_numbers) != 1
            or len(exact_numbers) != 1
            or associated_numbers != exact_numbers
        ):
            raise CancellationError(
                "event head does not resolve to exactly one associated pull request"
            )
        return next(iter(exact_numbers))

    def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
        repository = object.__getattribute__(self, "_GitHubApi__repository")
        payload = self._pull_request(pr_number)
        if (
            object.__getattribute__(self, "_GitHubApi__repository")
            != repository
        ):
            raise CancellationError("GitHub API repository binding changed")
        head = payload.get("head")
        base = payload.get("base")
        state = payload.get("state")
        draft = payload.get("draft")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise CancellationError("invalid pull request head/base")
        if state not in ("open", "closed") or type(draft) is not bool:
            raise CancellationError("invalid pull request qualification state")

        base_repo = base.get("repo")
        if not isinstance(base_repo, dict) or base_repo.get("full_name") != repository:
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

    def live_pr_head(self, pr_number: int) -> str:
        payload = self._pull_request(pr_number)
        head = payload.get("head")
        if not isinstance(head, dict):
            raise CancellationError("invalid pull request head")
        return _require_sha(head.get("sha"), field="live pull request head")

    def pr_is_integration_capable(self, pr_number: int) -> bool:
        return _pull_request_qualification_state(
            self.live_pr_qualification(pr_number)
        )[1]

    def _active_runs_for_status(
        self,
        status: str,
        *,
        _active_statuses: tuple[str, ...] = _ACTIVE_STATUSES,
        _runs_per_page: int = _RUNS_PER_PAGE,
        _encode_query=urlencode,
        _encode_query_code=urlencode.__code__,
        _run_parser=parse_run,
        _run_parser_code=parse_run.__code__,
    ) -> tuple[WorkflowRun, ...]:
        if (
            type(_active_statuses) is not tuple
            or not _active_statuses
            or status not in _active_statuses
        ):
            raise CancellationError("invalid active workflow status")
        if (
            type(_runs_per_page) is not int
            or _runs_per_page <= 0
            or _runs_per_page > 100
            or not callable(_encode_query)
            or getattr(_encode_query, "__code__", None) is not _encode_query_code
            or getattr(_run_parser, "__code__", None) is not _run_parser_code
        ):
            raise CancellationError("workflow-runs pagination authority is unavailable")

        request_impl = self._request
        request_func = getattr(request_impl, "__func__", request_impl)
        request_code = getattr(request_func, "__code__", None)

        def request_dispatch_current() -> bool:
            bound = self._request
            bound_func = getattr(bound, "__func__", bound)
            return (
                request_code is not None
                and bound_func is request_func
                and getattr(request_func, "__code__", None) is request_code
                and getattr(_encode_query, "__code__", None) is _encode_query_code
                and getattr(_run_parser, "__code__", None) is _run_parser_code
            )

        if not request_dispatch_current():
            raise CancellationError("workflow-runs request dispatch changed")

        runs: list[WorkflowRun] = []
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
            payload = request_impl(f"/actions/runs?{query}")
            if not request_dispatch_current():
                raise CancellationError("workflow-runs request dispatch changed")
            if (
                not isinstance(payload, dict)
                or type(payload.get("total_count")) is not int
                or payload["total_count"] < 0
                or not isinstance(payload.get("workflow_runs"), list)
            ):
                raise CancellationError("invalid workflow-runs response")
            page_runs = payload["workflow_runs"]
            runs.extend(_run_parser(item) for item in page_runs)
            total_count = payload["total_count"]
            if not page_runs or len(runs) >= total_count:
                break
            if len(page_runs) < _runs_per_page:
                raise CancellationError(
                    "workflow-runs pagination ended before reported total_count"
                )
            page += 1
        return tuple(runs)

    def active_runs(
        self,
        *,
        _active_statuses: tuple[str, ...] = _ACTIVE_STATUSES,
    ) -> tuple[WorkflowRun, ...]:
        if (
            type(_active_statuses) is not tuple
            or not _active_statuses
            or any(type(item) is not str or not item for item in _active_statuses)
        ):
            raise CancellationError("active workflow status authority is unavailable")
        status_reader = self._active_runs_for_status
        status_reader_func = getattr(status_reader, "__func__", status_reader)
        status_reader_code = getattr(status_reader_func, "__code__", None)
        if status_reader_code is None:
            raise CancellationError("active workflow reader authority is unavailable")
        runs: list[WorkflowRun] = []
        for status in _active_statuses:
            bound = self._active_runs_for_status
            if (
                getattr(bound, "__func__", bound) is not status_reader_func
                or getattr(status_reader_func, "__code__", None) is not status_reader_code
            ):
                raise CancellationError("active workflow reader authority changed")
            runs.extend(status_reader(status))
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

    def _build_cancel(
        request_impl,
        allowed_http_error_type,
        cancellation_accepted,
    ):
        request_impl_code = getattr(request_impl, "__code__", None)
        if request_impl_code is None:
            raise RuntimeError("canonical cancellation request executable is unavailable")

        def cancel(self, run_id: int) -> None:
            # The run id is the irreversible effect coordinate. Validate the primitive
            # directly inside the closure-built authority boundary so rebinding the
            # module-level compatibility helper cannot redirect a trusted cancellation.
            if type(run_id) is not int or run_id <= 0:
                raise CancellationError("invalid run id")
            bound_request = getattr(self, "_request", None)
            if (
                getattr(request_impl, "__code__", None) is not request_impl_code
                or getattr(bound_request, "__self__", None) is not self
                or getattr(bound_request, "__func__", None) is not request_impl
            ):
                raise CancellationError(
                    "workflow run cancellation request dispatch changed"
                )
            payload = request_impl(
                self,
                f"/actions/runs/{run_id}/cancel",
                method="POST",
                allowed_http_errors=frozenset({409}),
            )
            if isinstance(payload, allowed_http_error_type):
                if payload.status_code != 409:
                    raise CancellationError(
                        "unexpected allowed cancellation HTTP status"
                    )
                rebound_request = getattr(self, "_request", None)
                if (
                    getattr(request_impl, "__code__", None) is not request_impl_code
                    or getattr(rebound_request, "__self__", None) is not self
                    or getattr(rebound_request, "__func__", None) is not request_impl
                ):
                    raise CancellationError(
                        "workflow run cancellation request dispatch changed"
                    )
                status_payload = request_impl(
                    self,
                    f"/actions/runs/{run_id}",
                )
                if not isinstance(status_payload, dict):
                    raise CancellationError("invalid workflow-run response")
                status = status_payload.get("status")
                if status == "completed":
                    return
                if status not in _ACTIVE_STATUSES:
                    raise CancellationError("invalid workflow-run status")
                raise CancellationError(
                    "workflow run cancellation conflicted while run remains active"
                )
            if payload is not cancellation_accepted:
                raise CancellationError(
                    "workflow run cancellation missing HTTP 202 acceptance authority"
                )

        return cancel

    cancel = _build_cancel(_request, _AllowedHttpError, _CANCELLATION_ACCEPTED)
    del _build_cancel


def _qualification_snapshot(
    api: GitHubApi,
    pr_number: int,
    *,
    legacy_cancel_same_head: bool | None = None,
    _module_globals=globals(),
    _qualification_state_reader=_pull_request_qualification_state,
    _qualification_state_reader_code=_pull_request_qualification_state.__code__,
    _qualification_type=PullRequestQualification,
    _qualification_init=PullRequestQualification.__dict__["__init__"],
    _qualification_init_code=PullRequestQualification.__dict__["__init__"].__code__,
) -> tuple[str, bool]:
    """Read one qualification snapshot through definition-time authority anchors."""

    def snapshot_authority_current() -> bool:
        return (
            _module_globals.get("_pull_request_qualification_state")
            is _qualification_state_reader
            and getattr(_qualification_state_reader, "__code__", None)
            is _qualification_state_reader_code
            and _module_globals.get("PullRequestQualification") is _qualification_type
            and _qualification_type.__dict__.get("__init__") is _qualification_init
            and getattr(_qualification_init, "__code__", None)
            is _qualification_init_code
        )

    if not snapshot_authority_current():
        raise CancellationError("pull request qualification snapshot authority changed")

    resolver = getattr(api, "live_pr_qualification", None)
    if callable(resolver):
        qualification = resolver(pr_number)
    else:
        # Preserve the deliberately small fake API used by focused unit tests while
        # keeping the DTO constructor pinned across the external live-head read.
        qualification = _qualification_type(
            head_sha=api.live_pr_head(pr_number),
            integration_capable=not bool(legacy_cancel_same_head),
        )

    # A response/read callback may execute arbitrary same-process test or transport
    # hooks. Never resolve a rebound state reader or DTO constructor after that boundary.
    if not snapshot_authority_current():
        raise CancellationError("pull request qualification snapshot authority changed")
    return _qualification_state_reader(qualification)


def admit_current_head(
    *,
    api: GitHubApi,
    pr_number: int,
    event_head_sha: str,
    _module_globals=globals(),
    _qualification_snapshot_impl=_qualification_snapshot,
    _qualification_snapshot_code=_qualification_snapshot.__code__,
    _result_type=CancellationResult,
    _result_init=CancellationResult.__dict__["__init__"],
    _result_init_code=CancellationResult.__dict__["__init__"].__code__,
) -> CancellationResult:
    """Admit heavy work only for a current, live integration-capable PR snapshot."""

    if type(pr_number) is not int or pr_number <= 0:
        raise CancellationError("invalid pull request number")
    if type(event_head_sha) is not str or len(event_head_sha) != 40:
        raise CancellationError("invalid event head sha")
    event_head_sha = event_head_sha.lower()
    if any(ch not in "0123456789abcdef" for ch in event_head_sha):
        raise CancellationError("invalid event head sha")

    def admission_authority_current() -> bool:
        return (
            _module_globals.get("_qualification_snapshot")
            is _qualification_snapshot_impl
            and getattr(_qualification_snapshot_impl, "__code__", None)
            is _qualification_snapshot_code
            and _module_globals.get("CancellationResult") is _result_type
            and _result_type.__dict__.get("__init__") is _result_init
            and getattr(_result_init, "__code__", None) is _result_init_code
        )

    if not admission_authority_current():
        raise CancellationError("pull request admission authority changed")

    qualification = _qualification_snapshot_impl(api, pr_number)

    # The live qualification resolver crosses the GitHub transport boundary. Recheck
    # the definition-time helper/result authority before producing the workflow output.
    if not admission_authority_current():
        raise CancellationError("pull request admission authority changed")

    live_head_sha, integration_capable = qualification
    current_head = event_head_sha == live_head_sha and integration_capable

    # A positive admission unlocks expensive matrix allocation, so reread the exact
    # head/lifecycle tuple immediately before publishing that positive decision. Any
    # head move or ready/draft/close transition during admission revokes the output.
    if current_head:
        confirmation = _qualification_snapshot_impl(api, pr_number)
        if not admission_authority_current():
            raise CancellationError("pull request admission authority changed")
        current_head = confirmation == qualification

    return _result_type(
        current_head=current_head,
        cancelled_run_ids=(),
    )


def cancel_superseded(
    *,
    api: GitHubApi,
    pr_number: int,
    event_head_sha: str,
    workflow_name: str,
    current_run_id: int,
    cancel_same_head: bool | None = None,
) -> CancellationResult:
    event_head_sha = _require_sha(event_head_sha, field="event head sha")
    if cancel_same_head is not None and type(cancel_same_head) is not bool:
        raise CancellationError("cancel_same_head must be boolean or None")
    qualification = _qualification_snapshot(
        api, pr_number, legacy_cancel_same_head=cancel_same_head
    )
    live_head_sha, integration_capable = qualification
    if event_head_sha != live_head_sha:
        return CancellationResult(current_head=False, cancelled_run_ids=())
    derived_cancel_same_head = not integration_capable
    if cancel_same_head is not None and cancel_same_head != derived_cancel_same_head:
        raise CancellationError("cancel_same_head conflicts with live PR qualification")
    active_runs = api.active_runs()
    if _qualification_snapshot(
        api, pr_number, legacy_cancel_same_head=cancel_same_head
    ) != qualification:
        return CancellationResult(current_head=False, cancelled_run_ids=())
    selected = select_superseded_runs(
        active_runs,
        pr_number=pr_number,
        live_head_sha=live_head_sha,
        workflow_name=workflow_name,
        current_run_id=current_run_id,
        cancel_same_head=derived_cancel_same_head,
    )
    cancelled: list[int] = []
    for run_id in selected:
        # Head and lifecycle eligibility are one authority snapshot. If either changes
        # (including same-head draft/ready transitions), revoke cancellation authority
        # before the next irreversible POST.
        if _qualification_snapshot(
            api, pr_number, legacy_cancel_same_head=cancel_same_head
        ) != qualification:
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

    # Capture orchestration call targets before any GitHub transport callback can run.
    # A response hook may mutate module globals in-process; it must not be able to swap
    # the final workflow-output writer after admission has already consumed trusted data.
    admit_impl = admit_current_head
    cancel_impl = cancel_superseded
    output_writer = _write_github_output
    output_writer_code = getattr(output_writer, "__code__", None)

    try:
        api = GitHubApi(
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            token=os.environ.get("GITHUB_TOKEN", ""),
        )
        pr_number = args.pr_number
        if pr_number <= 0:
            if args.admission_only:
                raise CancellationError("admission requires an explicit pull request number")
            pr_number = api.associated_pr_number(args.event_head_sha)
        elif type(pr_number) is not int:
            raise CancellationError("invalid pull request number")

        if args.admission_only:
            result = admit_impl(
                api=api,
                pr_number=pr_number,
                event_head_sha=args.event_head_sha,
            )
        else:
            result = cancel_impl(
                api=api,
                pr_number=pr_number,
                event_head_sha=args.event_head_sha,
                workflow_name=args.workflow_name,
                current_run_id=args.current_run_id,
            )

        if (
            _write_github_output is not output_writer
            or output_writer_code is None
            or getattr(output_writer, "__code__", None) is not output_writer_code
        ):
            raise CancellationError("workflow output authority changed")
        output_writer(result)
    except CancellationError as exc:
        print(f"superseded-run cancellation failed: {exc}", file=sys.stderr)
        return 2
    cancelled = ",".join(str(item) for item in result.cancelled_run_ids)
    print("cancelled superseded workflow runs: " + cancelled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())