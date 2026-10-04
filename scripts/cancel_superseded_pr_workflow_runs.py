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
    # These values are cancellation-effect coordinates. Validate the primitives
    # directly here rather than resolving mutable module-level compatibility helpers.
    if type(pr_number) is not int or pr_number <= 0:
        raise CancellationError("invalid pull request number")
    if type(current_run_id) is not int or current_run_id <= 0:
        raise CancellationError("invalid current run id")
    if type(live_head_sha) is not str or len(live_head_sha) != 40:
        raise CancellationError("invalid live head sha")
    live_head_sha = live_head_sha.lower()
    if any(ch not in "0123456789abcdef" for ch in live_head_sha):
        raise CancellationError("invalid live head sha")
    if type(workflow_name) is not str or not workflow_name:
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
        request_type = Request
        urlopen_impl = urlopen

        def repository_binding_current() -> bool:
            return (
                object.__getattribute__(self, "_GitHubApi__repository")
                == repository
            )

        def transport_authority_current() -> bool:
            return Request is request_type and urlopen is urlopen_impl

        def assert_response_authority_current() -> None:
            if not repository_binding_current():
                raise CancellationError("GitHub API repository binding changed")
            if not transport_authority_current():
                raise CancellationError("GitHub API transport authority changed")

        if not repository_binding_current():
            raise CancellationError("GitHub API repository binding changed")
        if not transport_authority_current():
            raise CancellationError("GitHub API transport authority changed")

        is_cancel_request = (
            method == "POST"
            and path.startswith("/actions/runs/")
            and path.endswith("/cancel")
        )
        request = request_type(
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
            with urlopen_impl(request, timeout=20) as response:
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
                    assert_response_authority_current()
                    return _CANCELLATION_ACCEPTED
                if type(status_code) is not int or status_code != 200:
                    raise CancellationError(
                        "GitHub API GET returned unexpected HTTP status"
                    )
                body = response.read()
                assert_response_authority_current()
        except HTTPError as exc:
            try:
                assert_response_authority_current()
            except CancellationError as authority_exc:
                raise authority_exc from exc
            if exc.code in allowed_http_errors:
                return _AllowedHttpError(exc.code)
            raise CancellationError(
                f"GitHub API request failed: {type(exc).__name__}"
            ) from exc
        except (URLError, TimeoutError) as exc:
            try:
                assert_response_authority_current()
            except CancellationError as authority_exc:
                raise authority_exc from exc
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

        request_impl = self._request
        request_func = getattr(request_impl, "__func__", request_impl)
        request_self = getattr(request_impl, "__self__", None)
        request_code = getattr(request_func, "__code__", None)
        request_positional_defaults = getattr(request_func, "__defaults__", None)
        request_keyword_defaults = getattr(request_func, "__kwdefaults__", None)
        request_keyword_items = (
            tuple(request_keyword_defaults.items())
            if request_keyword_defaults is not None
            else ()
        )
        if request_code is None:
            raise CancellationError("pull request transport authority is unavailable")

        payload = request_impl(f"/pulls/{pr_number}")

        rebound_request = self._request
        rebound_func = getattr(rebound_request, "__func__", rebound_request)
        if (
            getattr(rebound_request, "__self__", None) is not request_self
            or rebound_func is not request_func
            or getattr(request_func, "__code__", None) is not request_code
            or getattr(request_func, "__defaults__", None)
            is not request_positional_defaults
            or getattr(request_func, "__kwdefaults__", None)
            is not request_keyword_defaults
            or (
                request_keyword_defaults is not None
                and (
                    len(request_keyword_defaults) != len(request_keyword_items)
                    or any(
                        key not in request_keyword_defaults
                        or request_keyword_defaults[key] is not value
                        for key, value in request_keyword_items
                    )
                )
            )
        ):
            raise CancellationError("pull request transport authority changed")
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
        _encode_query_defaults=urlencode.__defaults__,
        _encode_query_kwdefaults=urlencode.__kwdefaults__,
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
        request_positional_defaults = getattr(request_func, "__defaults__", None)
        request_keyword_defaults = getattr(request_func, "__kwdefaults__", None)
        request_keyword_items = (
            tuple(request_keyword_defaults.items())
            if request_keyword_defaults is not None
            else ()
        )

        def request_dispatch_current() -> bool:
            bound = self._request
            bound_func = getattr(bound, "__func__", bound)
            return (
                request_code is not None
                and bound_func is request_func
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
                and getattr(_sha_validator, "__code__", None) is _sha_validator_code
                and getattr(_positive_int, "__code__", None) is _positive_int_code
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
        pull_request_impl = self._pull_request
        pull_request_func = getattr(
            pull_request_impl,
            "__func__",
            pull_request_impl,
        )
        pull_request_self = getattr(pull_request_impl, "__self__", None)
        pull_request_code = getattr(pull_request_func, "__code__", None)
        if pull_request_code is None:
            raise CancellationError("pull request reader authority is unavailable")

        payload = pull_request_impl(pr_number)

        rebound_pull_request = self._pull_request
        rebound_pull_request_func = getattr(
            rebound_pull_request,
            "__func__",
            rebound_pull_request,
        )
        if (
            getattr(rebound_pull_request, "__self__", None) is not pull_request_self
            or rebound_pull_request_func is not pull_request_func
            or getattr(pull_request_func, "__code__", None) is not pull_request_code
        ):
            raise CancellationError("pull request reader authority changed")
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
        pull_request_impl = self._pull_request
        pull_request_func = getattr(
            pull_request_impl,
            "__func__",
            pull_request_impl,
        )
        pull_request_self = getattr(pull_request_impl, "__self__", None)
        pull_request_code = getattr(pull_request_func, "__code__", None)
        if pull_request_code is None:
            raise CancellationError("pull request reader authority is unavailable")

        payload = pull_request_impl(pr_number)

        rebound_pull_request = self._pull_request
        rebound_pull_request_func = getattr(
            rebound_pull_request,
            "__func__",
            rebound_pull_request,
        )
        if (
            getattr(rebound_pull_request, "__self__", None) is not pull_request_self
            or rebound_pull_request_func is not pull_request_func
            or getattr(pull_request_func, "__code__", None) is not pull_request_code
        ):
            raise CancellationError("pull request reader authority changed")
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
        _encode_query_defaults=urlencode.__defaults__,
        _encode_query_kwdefaults=urlencode.__kwdefaults__,
        _run_parser=parse_run,
        _run_parser_code=parse_run.__code__,
        _run_parser_defaults=parse_run.__defaults__,
        _run_parser_kwdefaults=parse_run.__kwdefaults__,
        _run_parser_kwdefault_items=tuple(parse_run.__kwdefaults__.items())
        if parse_run.__kwdefaults__ is not None
        else (),
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
            raise CancellationError("workflow-runs pagination authority is unavailable")

        request_impl = self._request
        request_func = getattr(request_impl, "__func__", request_impl)
        request_code = getattr(request_func, "__code__", None)
        request_positional_defaults = getattr(request_func, "__defaults__", None)
        request_keyword_defaults = getattr(request_func, "__kwdefaults__", None)
        request_keyword_items = (
            tuple(request_keyword_defaults.items())
            if request_keyword_defaults is not None
            else ()
        )

        def request_dispatch_current() -> bool:
            bound = self._request
            bound_func = getattr(bound, "__func__", bound)
            return (
                request_code is not None
                and bound_func is request_func
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
            )

        if not request_dispatch_current():
            raise CancellationError("workflow-runs request dispatch changed")

        runs: list[WorkflowRun] = []
        # total_count is a run count. Offset pagination can overlap when the active
        # queue grows ahead of the current page, so raw observation count is not a
        # completeness proof.
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
            total_count = payload["total_count"]
            if len(page_runs) > _runs_per_page:
                raise CancellationError("invalid workflow-runs page size")
            if total_count < len(page_runs):
                raise CancellationError("invalid workflow-runs total_count")
            for item in page_runs:
                run = _run_parser(item)
                runs.append(run)
                seen_run_ids.add(run.run_id)
            # Queue shrinkage can make a later page short even though an earlier
            # total_count was larger. Treat that as a safe terminal moving snapshot:
            # missed runs only defer cleanup, while forged extra rows fail closed above.
            if (
                not page_runs
                or len(page_runs) < _runs_per_page
                or len(seen_run_ids) >= total_count
            ):
                break
            page += 1
        return tuple(runs)

    def active_runs(
        self,
        *,
        _active_statuses: tuple[str, ...] = _ACTIVE_STATUSES,
        _status_reader=_active_runs_for_status,
        _status_reader_code=_active_runs_for_status.__code__,
        _status_reader_defaults=_active_runs_for_status.__defaults__,
        _status_reader_kwdefaults=_active_runs_for_status.__kwdefaults__,
        _status_reader_kwdefault_items=tuple(
            _active_runs_for_status.__kwdefaults__.items()
        ),
    ) -> tuple[WorkflowRun, ...]:
        if (
            type(_active_statuses) is not tuple
            or not _active_statuses
            or any(type(item) is not str or not item for item in _active_statuses)
        ):
            raise CancellationError("active workflow status authority is unavailable")

        def reader_authority_current() -> bool:
            bound = self._active_runs_for_status
            current_kwdefaults = getattr(_status_reader, "__kwdefaults__", None)
            return (
                getattr(_status_reader, "__code__", None) is _status_reader_code
                and getattr(_status_reader, "__defaults__", None)
                is _status_reader_defaults
                and current_kwdefaults is _status_reader_kwdefaults
                and current_kwdefaults is not None
                and len(current_kwdefaults) == len(_status_reader_kwdefault_items)
                and all(
                    key in current_kwdefaults and current_kwdefaults[key] is value
                    for key, value in _status_reader_kwdefault_items
                )
                and getattr(bound, "__self__", None) is self
                and getattr(bound, "__func__", None) is _status_reader
            )

        if not reader_authority_current():
            raise CancellationError("active workflow reader authority changed")
        runs: list[WorkflowRun] = []
        for status in _active_statuses:
            if not reader_authority_current():
                raise CancellationError("active workflow reader authority changed")
            runs.extend(_status_reader(self, status))
            if not reader_authority_current():
                raise CancellationError("active workflow reader authority changed")
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
        request_impl_defaults = getattr(request_impl, "__defaults__", None)
        initial_request_kwdefaults = getattr(request_impl, "__kwdefaults__", None)
        request_impl_kwdefaults_id = (
            id(initial_request_kwdefaults)
            if initial_request_kwdefaults is not None
            else None
        )
        request_impl_kwdefault_items = (
            tuple(initial_request_kwdefaults.items())
            if initial_request_kwdefaults is not None
            else ()
        )
        if request_impl_code is None:
            raise RuntimeError("canonical cancellation request executable is unavailable")

        def request_authority_current(self) -> bool:
            bound_request = getattr(self, "_request", None)
            current_kwdefaults = getattr(request_impl, "__kwdefaults__", None)
            return (
                getattr(request_impl, "__code__", None) is request_impl_code
                and getattr(request_impl, "__defaults__", None)
                is request_impl_defaults
                and (
                    (
                        request_impl_kwdefaults_id is None
                        and current_kwdefaults is None
                    )
                    or (
                        request_impl_kwdefaults_id is not None
                        and current_kwdefaults is not None
                        and id(current_kwdefaults) == request_impl_kwdefaults_id
                        and len(current_kwdefaults)
                        == len(request_impl_kwdefault_items)
                        and all(
                            key in current_kwdefaults
                            and current_kwdefaults[key] is value
                            for key, value in request_impl_kwdefault_items
                        )
                    )
                )
                and getattr(bound_request, "__self__", None) is self
                and getattr(bound_request, "__func__", None) is request_impl
            )

        def cancel(self, run_id: int) -> None:
            # The run id is the irreversible effect coordinate. Validate the primitive
            # directly inside the closure-built authority boundary so rebinding the
            # module-level compatibility helper cannot redirect a trusted cancellation.
            if type(run_id) is not int or run_id <= 0:
                raise CancellationError("invalid run id")
            if not request_authority_current(self):
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
                if not request_authority_current(self):
                    raise CancellationError(
                        "workflow run cancellation request dispatch changed"
                    )
                status_payload = request_impl(
                    self,
                    f"/actions/runs/{run_id}",
                )
                if not request_authority_current(self):
                    raise CancellationError(
                        "workflow run cancellation request dispatch changed"
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
            if not request_authority_current(self):
                raise CancellationError(
                    "workflow run cancellation request dispatch changed"
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
        resolver_func = getattr(resolver, "__func__", resolver)
        resolver_self = getattr(resolver, "__self__", None)
        resolver_code = getattr(resolver_func, "__code__", None)
        if resolver_code is None:
            raise CancellationError(
                "pull request qualification resolver authority is unavailable"
            )
        qualification = resolver(pr_number)
        rebound_resolver = getattr(api, "live_pr_qualification", None)
        rebound_func = getattr(rebound_resolver, "__func__", rebound_resolver)
        if (
            getattr(rebound_resolver, "__self__", None) is not resolver_self
            or rebound_func is not resolver_func
            or getattr(resolver_func, "__code__", None) is not resolver_code
        ):
            raise CancellationError(
                "pull request qualification resolver authority changed"
            )
    else:
        # Preserve the deliberately small fake API used by focused unit tests while
        # keeping the DTO constructor and live-head resolver pinned across the read.
        head_resolver = getattr(api, "live_pr_head", None)
        head_resolver_func = getattr(head_resolver, "__func__", head_resolver)
        head_resolver_self = getattr(head_resolver, "__self__", None)
        head_resolver_code = getattr(head_resolver_func, "__code__", None)
        if not callable(head_resolver) or head_resolver_code is None:
            raise CancellationError(
                "pull request live-head resolver authority is unavailable"
            )
        head_sha = head_resolver(pr_number)
        rebound_head_resolver = getattr(api, "live_pr_head", None)
        rebound_head_func = getattr(
            rebound_head_resolver,
            "__func__",
            rebound_head_resolver,
        )
        if (
            getattr(rebound_head_resolver, "__self__", None) is not head_resolver_self
            or rebound_head_func is not head_resolver_func
            or getattr(head_resolver_func, "__code__", None) is not head_resolver_code
        ):
            raise CancellationError(
                "pull request live-head resolver authority changed"
            )
        qualification = _qualification_type(
            head_sha=head_sha,
            integration_capable=not bool(legacy_cancel_same_head),
        )

    # A response/read callback may execute arbitrary same-process test or transport
    # hooks. Never resolve rebound state/DTO/resolver authority after that boundary.
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
    _qualification_snapshot_defaults=_qualification_snapshot.__defaults__,
    _qualification_snapshot_kwdefaults=_qualification_snapshot.__kwdefaults__,
    _qualification_snapshot_kwdefault_items=tuple(
        _qualification_snapshot.__kwdefaults__.items()
    ),
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

    def qualification_snapshot_metadata_current() -> bool:
        if (
            getattr(_qualification_snapshot_impl, "__defaults__", None)
            is not _qualification_snapshot_defaults
        ):
            return False
        current = getattr(_qualification_snapshot_impl, "__kwdefaults__", None)
        if current is not _qualification_snapshot_kwdefaults:
            return False
        if current is None or len(current) != len(_qualification_snapshot_kwdefault_items):
            return False
        return all(
            key in current and current[key] is value
            for key, value in _qualification_snapshot_kwdefault_items
        )

    def admission_authority_current() -> bool:
        return (
            _module_globals.get("_qualification_snapshot")
            is _qualification_snapshot_impl
            and getattr(_qualification_snapshot_impl, "__code__", None)
            is _qualification_snapshot_code
            and qualification_snapshot_metadata_current()
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
    _module_globals=globals(),
    _qualification_snapshot_impl=_qualification_snapshot,
    _qualification_snapshot_code=_qualification_snapshot.__code__,
    _qualification_snapshot_defaults=_qualification_snapshot.__defaults__,
    _qualification_snapshot_kwdefaults=_qualification_snapshot.__kwdefaults__,
    _qualification_snapshot_kwdefault_items=tuple(
        _qualification_snapshot.__kwdefaults__.items()
    ),
    _selector_impl=select_superseded_runs,
    _selector_code=select_superseded_runs.__code__,
    _result_type=CancellationResult,
    _result_init=CancellationResult.__dict__["__init__"],
    _result_init_code=CancellationResult.__dict__["__init__"].__code__,
    _production_api_type=GitHubApi,
    _production_active_runs=GitHubApi.active_runs,
    _production_active_runs_code=GitHubApi.active_runs.__code__,
    _production_active_runs_defaults=GitHubApi.active_runs.__defaults__,
    _production_active_runs_kwdefaults=GitHubApi.active_runs.__kwdefaults__,
    _production_active_runs_kwdefault_items=tuple(
        GitHubApi.active_runs.__kwdefaults__.items()
    ),
    _production_cancel=GitHubApi.cancel,
    _production_cancel_code=GitHubApi.cancel.__code__,
) -> CancellationResult:
    """Cancel only runs selected by the frozen live-PR/candidate authority graph."""

    def qualification_snapshot_metadata_current() -> bool:
        if (
            getattr(_qualification_snapshot_impl, "__defaults__", None)
            is not _qualification_snapshot_defaults
        ):
            return False
        current = getattr(_qualification_snapshot_impl, "__kwdefaults__", None)
        if current is not _qualification_snapshot_kwdefaults:
            return False
        if current is None or len(current) != len(_qualification_snapshot_kwdefault_items):
            return False
        return all(
            key in current and current[key] is value
            for key, value in _qualification_snapshot_kwdefault_items
        )

    def cancellation_authority_current() -> bool:
        if (
            _module_globals.get("_qualification_snapshot")
            is not _qualification_snapshot_impl
            or getattr(_qualification_snapshot_impl, "__code__", None)
            is not _qualification_snapshot_code
            or not qualification_snapshot_metadata_current()
            or _module_globals.get("select_superseded_runs") is not _selector_impl
            or getattr(_selector_impl, "__code__", None) is not _selector_code
            or _module_globals.get("CancellationResult") is not _result_type
            or _result_type.__dict__.get("__init__") is not _result_init
            or getattr(_result_init, "__code__", None) is not _result_init_code
        ):
            return False
        if type(api) is _production_api_type:
            return (
                _production_api_type.__dict__.get("active_runs")
                is _production_active_runs
                and getattr(_production_active_runs, "__code__", None)
                is _production_active_runs_code
                and getattr(_production_active_runs, "__defaults__", None)
                is _production_active_runs_defaults
                and getattr(_production_active_runs, "__kwdefaults__", None)
                is _production_active_runs_kwdefaults
                and _production_active_runs_kwdefaults is not None
                and len(_production_active_runs_kwdefaults)
                == len(_production_active_runs_kwdefault_items)
                and all(
                    key in _production_active_runs_kwdefaults
                    and _production_active_runs_kwdefaults[key] is value
                    for key, value in _production_active_runs_kwdefault_items
                )
                and _production_api_type.__dict__.get("cancel")
                is _production_cancel
                and getattr(_production_cancel, "__code__", None)
                is _production_cancel_code
            )
        return True

    active_runs = getattr(api, "active_runs", None)
    active_runs_func = getattr(active_runs, "__func__", active_runs)
    active_runs_self = getattr(active_runs, "__self__", None)
    active_runs_code = getattr(active_runs_func, "__code__", None)
    cancel = getattr(api, "cancel", None)
    cancel_func = getattr(cancel, "__func__", cancel)
    cancel_self = getattr(cancel, "__self__", None)
    cancel_code = getattr(cancel_func, "__code__", None)

    def api_dispatch_current() -> bool:
        rebound_active = getattr(api, "active_runs", None)
        rebound_cancel = getattr(api, "cancel", None)
        if type(api) is _production_api_type and (
            active_runs_self is not api
            or active_runs_func is not _production_active_runs
            or cancel_self is not api
            or cancel_func is not _production_cancel
        ):
            return False
        return (
            callable(active_runs)
            and active_runs_code is not None
            and getattr(rebound_active, "__self__", None) is active_runs_self
            and getattr(rebound_active, "__func__", rebound_active) is active_runs_func
            and getattr(active_runs_func, "__code__", None) is active_runs_code
            and callable(cancel)
            and cancel_code is not None
            and getattr(rebound_cancel, "__self__", None) is cancel_self
            and getattr(rebound_cancel, "__func__", rebound_cancel) is cancel_func
            and getattr(cancel_func, "__code__", None) is cancel_code
        )

    if not cancellation_authority_current() or not api_dispatch_current():
        raise CancellationError("superseded-run cancellation authority changed")
    if type(pr_number) is not int or pr_number <= 0:
        raise CancellationError("invalid pull request number")
    if type(current_run_id) is not int or current_run_id <= 0:
        raise CancellationError("invalid current run id")
    if type(event_head_sha) is not str or len(event_head_sha) != 40:
        raise CancellationError("invalid event head sha")
    event_head_sha = event_head_sha.lower()
    if any(ch not in "0123456789abcdef" for ch in event_head_sha):
        raise CancellationError("invalid event head sha")
    if type(workflow_name) is not str or not workflow_name:
        raise CancellationError("workflow name is required")
    if cancel_same_head is not None and type(cancel_same_head) is not bool:
        raise CancellationError("cancel_same_head must be boolean or None")

    qualification = _qualification_snapshot_impl(
        api, pr_number, legacy_cancel_same_head=cancel_same_head
    )
    if not cancellation_authority_current() or not api_dispatch_current():
        raise CancellationError("superseded-run cancellation authority changed")
    live_head_sha, integration_capable = qualification
    if event_head_sha != live_head_sha:
        return _result_type(current_head=False, cancelled_run_ids=())
    derived_cancel_same_head = not integration_capable
    if cancel_same_head is not None and cancel_same_head != derived_cancel_same_head:
        raise CancellationError("cancel_same_head conflicts with live PR qualification")

    active_run_snapshot = active_runs()
    if not cancellation_authority_current() or not api_dispatch_current():
        raise CancellationError("superseded-run cancellation authority changed")
    confirmation = _qualification_snapshot_impl(
        api, pr_number, legacy_cancel_same_head=cancel_same_head
    )
    if not cancellation_authority_current() or not api_dispatch_current():
        raise CancellationError("superseded-run cancellation authority changed")
    if confirmation != qualification:
        return _result_type(current_head=False, cancelled_run_ids=())

    selected = _selector_impl(
        active_run_snapshot,
        pr_number=pr_number,
        live_head_sha=live_head_sha,
        workflow_name=workflow_name,
        current_run_id=current_run_id,
        cancel_same_head=derived_cancel_same_head,
    )
    if not cancellation_authority_current() or not api_dispatch_current():
        raise CancellationError("superseded-run cancellation authority changed")

    cancelled: list[int] = []
    for run_id in selected:
        # Head and lifecycle eligibility are one authority snapshot. If either changes
        # (including same-head draft/ready transitions), revoke cancellation authority
        # before the next irreversible POST.
        confirmation = _qualification_snapshot_impl(
            api, pr_number, legacy_cancel_same_head=cancel_same_head
        )
        if not cancellation_authority_current() or not api_dispatch_current():
            raise CancellationError("superseded-run cancellation authority changed")
        if confirmation != qualification:
            return _result_type(
                current_head=False,
                cancelled_run_ids=tuple(cancelled),
            )
        cancel(run_id)
        if not cancellation_authority_current() or not api_dispatch_current():
            raise CancellationError("superseded-run cancellation authority changed")
        cancelled.append(run_id)
    return _result_type(current_head=True, cancelled_run_ids=tuple(cancelled))


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


def _build_main(*, module_globals, admit_impl, cancel_impl, output_writer, api_type):
    """Freeze controller orchestration roots in closure-owned composition state."""

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

    admit_code = getattr(admit_impl, "__code__", None)
    cancel_code = getattr(cancel_impl, "__code__", None)
    output_writer_code = getattr(output_writer, "__code__", None)
    api_init = api_type.__dict__.get("__init__")
    api_init_code = getattr(api_init, "__code__", None)
    api_method_snapshots = tuple(
        (name, api_type.__dict__.get(name))
        for name in (
            "_request",
            "_pull_request",
            "live_pr_qualification",
            "associated_pr_number",
        )
    )
    api_method_witnesses = tuple(
        (
            name,
            method,
            getattr(method, "__code__", None),
            freeze_default_metadata(method),
        )
        for name, method in api_method_snapshots
    )
    request_type = module_globals.get("Request")
    request_init = request_type.__dict__.get("__init__")
    request_init_code = getattr(request_init, "__code__", None)
    urlopen_impl = module_globals.get("urlopen")
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
    admit_defaults = freeze_default_metadata(admit_impl)
    cancel_defaults = freeze_default_metadata(cancel_impl)
    output_writer_defaults = freeze_default_metadata(output_writer)
    api_init_defaults = freeze_default_metadata(api_init)
    parser_init_defaults = freeze_default_metadata(parser_init)
    parser_add_argument_defaults = freeze_default_metadata(parser_add_argument)
    parser_parse_args_defaults = freeze_default_metadata(parser_parse_args)

    def main(argv: list[str] | None) -> int:
        def orchestration_authority_current() -> bool:
            return (
                module_globals.get("admit_current_head") is admit_impl
                and getattr(admit_impl, "__code__", None) is admit_code
                and default_metadata_current(admit_impl, admit_defaults)
                and module_globals.get("cancel_superseded") is cancel_impl
                and getattr(cancel_impl, "__code__", None) is cancel_code
                and default_metadata_current(cancel_impl, cancel_defaults)
                and module_globals.get("_write_github_output") is output_writer
                and getattr(output_writer, "__code__", None) is output_writer_code
                and default_metadata_current(output_writer, output_writer_defaults)
                and module_globals.get("GitHubApi") is api_type
                and api_type.__dict__.get("__init__") is api_init
                and getattr(api_init, "__code__", None) is api_init_code
                and default_metadata_current(api_init, api_init_defaults)
                and all(
                    api_type.__dict__.get(name) is method
                    and getattr(method, "__code__", None) is code
                    and default_metadata_current(method, defaults)
                    for name, method, code, defaults in api_method_witnesses
                )
                and module_globals.get("Request") is request_type
                and request_type.__dict__.get("__init__") is request_init
                and getattr(request_init, "__code__", None) is request_init_code
                and module_globals.get("urlopen") is urlopen_impl
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
            )

        try:
            if not orchestration_authority_current():
                raise CancellationError("main orchestration authority changed")

            parser = parser_type()
            parser_add_argument(parser, "--pr-number", type=int, required=True)
            parser_add_argument(parser, "--event-head-sha", required=True)
            parser_add_argument(parser, "--workflow-name", required=True)
            parser_add_argument(parser, "--current-run-id", type=int, required=True)
            parser_add_argument(parser, "--admission-only", action="store_true")
            args = parser_parse_args(parser, argv)

            if not orchestration_authority_current():
                raise CancellationError("main orchestration authority changed")

            api = api_type(
                repository=environment.get("GITHUB_REPOSITORY", ""),
                token=environment.get("GITHUB_TOKEN", ""),
            )
            if not orchestration_authority_current():
                raise CancellationError("main orchestration authority changed")

            pr_number = args.pr_number
            if pr_number <= 0:
                if args.admission_only:
                    raise CancellationError("admission requires an explicit pull request number")
                pr_number = api.associated_pr_number(args.event_head_sha)
            elif type(pr_number) is not int:
                raise CancellationError("invalid pull request number")

            if not orchestration_authority_current():
                raise CancellationError("main orchestration authority changed")

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

            if not orchestration_authority_current():
                raise CancellationError("main orchestration authority changed")
            output_writer(result)
        except CancellationError as exc:
            print(f"superseded-run cancellation failed: {exc}", file=sys.stderr)
            return 2
        cancelled = ",".join(str(item) for item in result.cancelled_run_ids)
        print("cancelled superseded workflow runs: " + cancelled)
        return 0



    return main


main = _build_main(
    module_globals=globals(),
    admit_impl=admit_current_head,
    cancel_impl=cancel_superseded,
    output_writer=_write_github_output,
    api_type=GitHubApi,
)
del _build_main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))