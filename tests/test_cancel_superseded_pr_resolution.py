from __future__ import annotations

import pytest

from scripts.cancel_superseded_pr_workflow_runs import CancellationError, GitHubApi


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
