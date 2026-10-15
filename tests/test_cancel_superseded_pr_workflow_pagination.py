from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from scripts.cancel_superseded_pr_workflow_runs import GitHubApi


HEAD = "a" * 40


def _payload(run_id: int, status: str) -> dict[str, object]:
    return {
        "id": run_id,
        "head_sha": HEAD,
        "name": "CI",
        "status": status,
        "pull_requests": [{"number": 2008}],
    }


def test_active_runs_paginates_every_reported_active_status(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    requested: list[tuple[str, int]] = []

    def fake_request(path: str, *, method: str = "GET") -> object:
        assert method == "GET"
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        status = query["status"][0]
        page = int(query["page"][0])
        requested.append((status, page))
        if status == "queued":
            if page == 1:
                return {
                    "total_count": 101,
                    "workflow_runs": [_payload(index, status) for index in range(1, 101)],
                }
            assert page == 2
            return {
                "total_count": 101,
                "workflow_runs": [_payload(101, status)],
            }
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(api, "_request", fake_request)

    runs = api.active_runs()

    assert len(runs) == 101
    assert runs[-1].run_id == 101
    assert ("queued", 2) in requested
    for status in ("in_progress", "waiting", "pending", "requested"):
        assert (status, 1) in requested
