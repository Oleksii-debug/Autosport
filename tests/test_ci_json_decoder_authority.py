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


def test_request_rejects_inflight_json_decoder_init_code_mutation(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    decoder_init = controller_module.json.JSONDecoder.__dict__["__init__"]

    def forged_init(self, *args, **kwargs) -> None:
        raise AssertionError("forged decoder constructor must not execute")

    class MutatingResponse(_Response):
        def read(self) -> bytes:
            monkeypatch.setattr(decoder_init, "__code__", forged_init.__code__)
            return super().read()

    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: MutatingResponse(),
    )

    with pytest.raises(
        CancellationError,
        match="GitHub API JSON parser authority changed",
    ):
        api._request("/actions/runs/123")


def test_request_rejects_inflight_json_decoder_decode_code_mutation(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    decoder_decode = controller_module.json.JSONDecoder.__dict__["decode"]

    def forged_decode(self, value):
        return {"status": "forged"}

    class MutatingResponse(_Response):
        def read(self) -> bytes:
            monkeypatch.setattr(decoder_decode, "__code__", forged_decode.__code__)
            return super().read()

    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: MutatingResponse(),
    )

    with pytest.raises(
        CancellationError,
        match="GitHub API JSON parser authority changed",
    ):
        api._request("/actions/runs/123")

def test_request_rejects_inflight_json_decoder_scanner_global_rebind(
    monkeypatch,
) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    decoder_globals = controller_module.json.JSONDecoder.__dict__["__init__"].__globals__
    forged_calls: list[str] = []

    class ForgedScannerModule:
        @staticmethod
        def make_scanner(_decoder):
            forged_calls.append("make_scanner")

            def scan_once(value, index):
                forged_calls.append("scan_once")
                return {"status": "forged"}, len(value)

            return scan_once

    class RebindingResponse(_Response):
        def read(self) -> bytes:
            monkeypatch.setitem(decoder_globals, "scanner", ForgedScannerModule)
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


def test_request_rejects_inflight_json_decoder_make_scanner_rebind(
    monkeypatch,
) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    decoder_globals = controller_module.json.JSONDecoder.__dict__["__init__"].__globals__
    scanner_module = decoder_globals["scanner"]
    forged_calls: list[str] = []

    def forged_make_scanner(_decoder):
        forged_calls.append("make_scanner")

        def scan_once(value, index):
            forged_calls.append("scan_once")
            return {"status": "forged"}, len(value)

        return scan_once

    class RebindingResponse(_Response):
        def read(self) -> bytes:
            monkeypatch.setattr(scanner_module, "make_scanner", forged_make_scanner)
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


def test_request_rejects_inflight_json_object_parser_code_mutation(
    monkeypatch,
) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    decoder_globals = controller_module.json.JSONDecoder.__dict__["__init__"].__globals__
    json_object = decoder_globals["JSONObject"]

    def forged_json_object(*_args, **_kwargs):
        raise AssertionError("forged object parser must not execute")

    class MutatingResponse(_Response):
        def read(self) -> bytes:
            monkeypatch.setattr(json_object, "__code__", forged_json_object.__code__)
            return super().read()

    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: MutatingResponse(),
    )

    with pytest.raises(
        CancellationError,
        match="GitHub API JSON parser authority changed",
    ):
        api._request("/actions/runs/123")


def test_request_rejects_inflight_json_array_parser_code_mutation(
    monkeypatch,
) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    decoder_globals = controller_module.json.JSONDecoder.__dict__["__init__"].__globals__
    json_array = decoder_globals["JSONArray"]

    def forged_json_array(*_args, **_kwargs):
        raise AssertionError("forged array parser must not execute")

    class MutatingResponse(_Response):
        def read(self) -> bytes:
            monkeypatch.setattr(json_array, "__code__", forged_json_array.__code__)
            return super().read()

    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: MutatingResponse(),
    )

    with pytest.raises(
        CancellationError,
        match="GitHub API JSON parser authority changed",
    ):
        api._request("/actions/runs/123")


def test_request_rejects_inflight_json_scanstring_rebind(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    decoder_globals = controller_module.json.JSONDecoder.__dict__["__init__"].__globals__

    def forged_scanstring(*_args, **_kwargs):
        raise AssertionError("forged string parser must not execute")

    class RebindingResponse(_Response):
        def read(self) -> bytes:
            monkeypatch.setitem(decoder_globals, "scanstring", forged_scanstring)
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

