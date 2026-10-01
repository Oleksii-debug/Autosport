from __future__ import annotations

import pytest

from scripts.cancel_superseded_pr_workflow_runs import (
    CancellationError,
    GitHubApi,
    PullRequestQualification,
)


HEAD_A = "a" * 40
HEAD_B = "b" * 40


class FakeAssociatedPullsApi(GitHubApi):
    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self._pages = list(pages)
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
        if not self._pages:
            raise AssertionError("unexpected commit-association request")
        return self._pages.pop(0)


def _pr(number: int, head_sha: str) -> dict[str, object]:
    return {"number": number, "head": {"sha": head_sha}}


def test_missing_workflow_run_pr_number_resolves_one_unambiguous_associated_head() -> None:
    api = FakeAssociatedPullsApi([[_pr(2022, HEAD_A)]])

    assert api.associated_pr_number(HEAD_A) == 2022
    assert api.paths == [f"/commits/{HEAD_A}/pulls?per_page=100&page=1"]


def test_missing_workflow_run_pr_number_rejects_historical_cross_pr_commit_reuse() -> None:
    api = FakeAssociatedPullsApi([[_pr(2022, HEAD_A), _pr(1999, HEAD_B)]])

    with pytest.raises(CancellationError, match="exactly one associated pull request"):
        api.associated_pr_number(HEAD_A)


def test_missing_workflow_run_pr_number_fails_closed_on_ambiguous_exact_head() -> None:
    api = FakeAssociatedPullsApi([[ _pr(2022, HEAD_A), _pr(2023, HEAD_A) ]])

    with pytest.raises(CancellationError, match="exactly one associated pull request"):
        api.associated_pr_number(HEAD_A)


def test_missing_workflow_run_pr_number_fails_closed_without_exact_head() -> None:
    api = FakeAssociatedPullsApi([[ _pr(2022, HEAD_B) ]])

    with pytest.raises(CancellationError, match="exactly one associated pull request"):
        api.associated_pr_number(HEAD_A)


class FakeQualificationApi(GitHubApi):
    def __init__(self, payload: dict[str, object]) -> None:
        self._repository = "Oleksii-debug/Autosport"
        self._payload = payload

    def _pull_request(self, pr_number: int) -> dict[str, object]:
        assert pr_number == 2022
        return self._payload


def _qualification_pr(
    *,
    head_repo: str = "Oleksii-debug/Autosport",
    base_repo: str = "Oleksii-debug/Autosport",
    state: str = "open",
    draft: bool = False,
) -> dict[str, object]:
    return {
        "state": state,
        "draft": draft,
        "head": {
            "sha": HEAD_A,
            "repo": {"full_name": head_repo},
        },
        "base": {
            "repo": {"full_name": base_repo},
        },
    }


def test_live_qualification_accepts_open_nondraft_same_repository_head() -> None:
    api = FakeQualificationApi(_qualification_pr())

    assert api.live_pr_qualification(2022) == PullRequestQualification(
        head_sha=HEAD_A,
        integration_capable=True,
    )


def test_live_qualification_fails_closed_for_fork_head() -> None:
    api = FakeQualificationApi(
        _qualification_pr(head_repo="external-contributor/Autosport")
    )

    assert api.live_pr_qualification(2022) == PullRequestQualification(
        head_sha=HEAD_A,
        integration_capable=False,
    )


def test_live_qualification_rejects_foreign_base_repository() -> None:
    api = FakeQualificationApi(
        _qualification_pr(base_repo="external-owner/Autosport")
    )

    with pytest.raises(CancellationError, match="base repository is not canonical"):
        api.live_pr_qualification(2022)
