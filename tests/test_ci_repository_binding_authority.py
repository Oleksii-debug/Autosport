from __future__ import annotations

import pytest

import scripts.cancel_superseded_pr_workflow_runs as base_controller
import scripts.cancel_superseded_pr_workflow_runs_scoped as scoped_controller
from scripts.cancel_superseded_pr_workflow_runs import CancellationError
from scripts.cancel_superseded_pr_workflow_runs_scoped import (
    WorkflowScopedGitHubApi,
    _trusted_live_pr_qualification,
)


HEAD = "a" * 40


def test_repository_coordinate_rejects_direct_instance_rebind() -> None:
    api = base_controller.GitHubApi(repository="owner/repo", token="token")

    with pytest.raises(AttributeError):
        api._repository = "other/repo"  # type: ignore[misc]

    assert api._repository == "owner/repo"


def test_base_live_qualification_rejects_repository_drift_during_read(
    monkeypatch,
) -> None:
    api = base_controller.GitHubApi(repository="owner/repo", token="token")

    def drifting_pull_request(pr_number: int):
        assert pr_number == 2008
        api.__dict__["_GitHubApi__repository"] = "other/repo"
        return {
            "state": "open",
            "draft": False,
            "head": {
                "sha": HEAD,
                "repo": {"full_name": "other/repo"},
            },
            "base": {"repo": {"full_name": "other/repo"}},
        }

    monkeypatch.setattr(api, "_pull_request", drifting_pull_request)

    with pytest.raises(
        CancellationError,
        match="GitHub API repository binding changed",
    ):
        api.live_pr_qualification(2008)


def test_scoped_live_qualification_rejects_repository_drift_during_read(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"state":"open","draft":false,'
                b'"head":{"sha":"'
                + HEAD.encode("ascii")
                + b'","repo":{"full_name":"other/repo"}},'
                b'"base":{"repo":{"full_name":"other/repo"}}}'
            )

    def drifting_urlopen(request, *, timeout: int):
        assert timeout == 20
        assert request.full_url == "https://api.github.com/repos/owner/repo/pulls/303"
        api.__dict__["_GitHubApi__repository"] = "other/repo"
        return FakeResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        drifting_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="GitHub API repository binding changed",
    ):
        _trusted_live_pr_qualification(api, 303)
