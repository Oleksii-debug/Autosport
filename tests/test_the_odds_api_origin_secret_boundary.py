from urllib.error import HTTPError

import pytest

import autosport.the_odds_api_provider as odds_api_module
from autosport.the_odds_api_provider import (
    TheOddsApiProvider,
    TheOddsApiTransportError,
)


@pytest.mark.parametrize(
    "base_url",
    (
        "https://attacker.example",
        "https://api.the-odds-api.com.attacker.example",
        "https://api.the-odds-api.com@attacker.example",
        "https://api.the-odds-api.com:444",
        "http://api.the-odds-api.com",
    ),
)
def test_credential_bearing_provider_rejects_noncanonical_origin(base_url: str) -> None:
    """A caller-selected origin must never receive The Odds API credentials."""

    with pytest.raises(ValueError, match="canonical The Odds API"):
        TheOddsApiProvider(
            "SECRET_SENTINEL_DO_NOT_SEND",
            sport="soccer_epl",
            base_url=base_url,
        )


def test_post_construction_origin_mutation_fails_before_network_dispatch(monkeypatch) -> None:
    """Mutable object state cannot reroute a real credential to another host."""

    opened: list[str] = []

    class FakeOpener:
        def open(self, request, timeout):  # pragma: no cover - must never run
            del timeout
            opened.append(request.full_url)
            raise AssertionError("noncanonical origin reached network dispatch")

    monkeypatch.setattr(odds_api_module, "build_opener", lambda *_: FakeOpener())
    provider = TheOddsApiProvider(
        "SECRET_SENTINEL_DO_NOT_SEND",
        sport="soccer_epl",
    )
    provider.base_url = "https://attacker.example"

    with pytest.raises(TheOddsApiTransportError, match="non-canonical provider URL"):
        provider.read_batch()

    assert opened == []


def test_http_error_does_not_echo_credential_bearing_url(monkeypatch) -> None:
    """Transport errors expose bounded status, never the secret-bearing request URL."""

    class FakeOpener:
        def open(self, request, timeout):
            del timeout
            raise HTTPError(
                request.full_url,
                401,
                f"upstream rejected {request.full_url}",
                {},
                None,
            )

    monkeypatch.setattr(odds_api_module, "build_opener", lambda *_: FakeOpener())
    provider = TheOddsApiProvider(
        "SECRET_SENTINEL_DO_NOT_SEND",
        sport="soccer_epl",
    )

    with pytest.raises(TheOddsApiTransportError) as captured:
        provider.read_batch()

    rendered = str(captured.value)
    assert rendered == "The Odds API HTTP 401"
    assert "SECRET_SENTINEL_DO_NOT_SEND" not in rendered
    assert "apiKey=" not in rendered


@pytest.mark.parametrize("status", (301, 302, 303, 307, 308))
def test_redirect_handler_refuses_credential_bearing_second_hop(status: int) -> None:
    """Every standard redirect status is fail-closed before a second request is built."""

    handler = odds_api_module._RejectRedirects()
    redirected = handler.redirect_request(
        object(),
        object(),
        status,
        "redirect",
        {},
        "https://attacker.example/steal",
    )

    assert redirected is None
