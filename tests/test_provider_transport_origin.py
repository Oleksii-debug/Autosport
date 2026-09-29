from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request

import pytest

import autosport._provider_transport_origin as transport
import autosport.provider_observation_authority as provider


class _Response:
    status = 200
    headers = {"Content-Type": "text/event-stream; charset=utf-8"}

    def __init__(self, frame: dict[str, object]) -> None:
        encoded = json.dumps(frame, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self._lines = [b"event: initial_state\n", b"data: " + encoded + b"\n", b"\n"]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def __iter__(self):
        return iter(self._lines)


class _Opener:
    def __init__(self, result) -> None:
        self.result = result
        self.requests: list[tuple[str, str | None, float]] = []

    def open(self, request, *, timeout):
        self.requests.append((request.full_url, request.get_header("X-api-key"), timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _scope() -> provider.CompleteGameBoardRequest:
    return provider.CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("bovada",),
    )


def _complete_empty_frame() -> dict[str, object]:
    return {
        "type": "initial_state",
        "sport_key": "table_tennis",
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "partial": False,
        "count": 0,
        "data": [],
    }


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_provider_redirect_handler_never_constructs_follow_up_request(code):
    handler = transport._RejectProviderRedirects()
    original = Request(
        "https://parlay-api.com/v1/sse/odds/table_tennis",
        headers={"X-API-Key": "secret-value"},
    )

    redirected = handler.redirect_request(
        original,
        None,
        code,
        "redirect",
        {"Location": "https://attacker.example/collect"},
        "https://attacker.example/collect",
    )

    assert redirected is None


def test_provider_transport_rejects_noncanonical_origin_before_open(monkeypatch):
    called = False

    def fail_if_built(*handlers):
        nonlocal called
        del handlers
        called = True
        raise AssertionError("transport must not be built for a noncanonical origin")

    monkeypatch.setattr(transport, "build_opener", fail_if_built)

    with pytest.raises(
        provider.ProviderObservationUnsupportedError,
        match="canonical Parlay HTTPS endpoint",
    ):
        provider.urlopen(
            Request(
                "https://attacker.example/v1/sse/odds/table_tennis",
                headers={"X-API-Key": "secret-value"},
            ),
            timeout=1.0,
        )

    assert called is False


def test_canonical_provider_transport_installs_no_redirect_opener(monkeypatch):
    response = object()
    opener = _Opener(response)
    installed_handlers: tuple[object, ...] = ()

    def fake_build_opener(*handlers):
        nonlocal installed_handlers
        installed_handlers = handlers
        return opener

    monkeypatch.setattr(transport, "build_opener", fake_build_opener)
    request = Request(
        "https://parlay-api.com/v1/sse/odds/table_tennis?limit=1000",
        headers={"X-API-Key": "secret-value"},
        method="GET",
    )

    assert provider.urlopen(request, timeout=2.5) is response
    assert len(installed_handlers) == 1
    assert isinstance(installed_handlers[0], transport._RejectProviderRedirects)
    assert opener.requests == [
        (
            "https://parlay-api.com/v1/sse/odds/table_tennis?limit=1000",
            "secret-value",
            2.5,
        )
    ]


def test_redirect_response_fails_closed_before_any_redirect_target_request(monkeypatch):
    canonical_url = _scope().sse_url()
    error = HTTPError(
        canonical_url,
        302,
        "Found",
        {"Location": "https://attacker.example/collect"},
        None,
    )
    opener = _Opener(error)
    monkeypatch.setattr(transport, "build_opener", lambda *handlers: opener)

    with pytest.raises(
        provider.ProviderObservationUnsupportedError,
        match="initial-state acquisition failed",
    ):
        provider.capture_parlay_complete_game_board(
            api_key="secret-value",
            request=_scope(),
            timeout_seconds=3.0,
        )

    assert opener.requests == [(canonical_url, "secret-value", 3.0)]
    assert all("attacker.example" not in url for url, _, _ in opener.requests)


def test_exact_origin_https_200_sse_still_mints_ephemeral_authority(monkeypatch):
    opener = _Opener(_Response(_complete_empty_frame()))
    installed_handlers: tuple[object, ...] = ()

    def fake_build_opener(*handlers):
        nonlocal installed_handlers
        installed_handlers = handlers
        return opener

    monkeypatch.setattr(transport, "build_opener", fake_build_opener)
    snapshot = provider.capture_parlay_complete_game_board(
        api_key="secret-value",
        request=_scope(),
        timeout_seconds=3.0,
    )

    provider.assert_complete_game_board_authoritative(snapshot)
    assert snapshot.frame["snapshot_complete"] is True
    assert len(installed_handlers) == 1
    assert isinstance(installed_handlers[0], transport._RejectProviderRedirects)
    assert opener.requests == [(_scope().sse_url(), "secret-value", 3.0)]
