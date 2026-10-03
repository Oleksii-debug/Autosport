from __future__ import annotations

import pytest

import scripts.cancel_superseded_pr_workflow_runs as controller_module
from scripts.cancel_superseded_pr_workflow_runs import CancellationError, GitHubApi


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return b'{"status":"completed"}'


def test_request_rejects_inflight_json_decoder_global_rebind(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    forged_calls: list[str] = []

    class ForgedDecoder:
        def __init__(self, *args, **kwargs) -> None:
            forged_calls.append("init")

        def decode(self, value):
            forged_calls.append("decode")
            return {"status": "forged"}

    class RebindingResponse(_Response):
        def read(self) -> bytes:
            monkeypatch.setattr(controller_module.json, "JSONDecoder", ForgedDecoder)
            return super().read()

    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: RebindingResponse(),
    )

    with pytest.raises(
        CancellationError,
        match="GitHub API JSON parser authority changed",
    ):
        api._request("/actions/runs/123")

    assert forged_calls == []
