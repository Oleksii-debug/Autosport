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


def test_post_construction_origin_mutation_fails_before_network_dispatch() -> None:
    """Mutable object state cannot reroute a real credential to another host."""

    provider = TheOddsApiProvider(
        "SECRET_SENTINEL_DO_NOT_SEND",
        sport="soccer_epl",
    )
    provider.base_url = "https://attacker.example"

    with pytest.raises(TheOddsApiTransportError, match="non-canonical provider URL"):
        provider.read_batch()
def test_http_error_does_not_echo_credential_bearing_url() -> None:
    """Injected lower-seam errors are sanitized without becoming provider authority."""

    secret = "SECRET_SENTINEL_DO_NOT_SEND"
    url = (
        "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
        f"?apiKey={secret}&regions=eu&markets=h2h"
    )

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

    with pytest.raises(TheOddsApiTransportError) as captured:
        odds_api_module._perform_http_json_response(
            url,
            1.0,
            opener_factory=lambda *_: FakeOpener(),
            proxy_handler_factory=odds_api_module.ProxyHandler,
            request_factory=odds_api_module.Request,
        )

    rendered = str(captured.value)
    assert rendered == "The Odds API HTTP 401"
    assert secret not in rendered
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
