from __future__ import annotations

import urllib.request as urllib_request

import pytest

import autosport.betfair_historical_entitlement as historical_module
from autosport.betfair_historical_entitlement import UrllibBetfairHistoricalTransport


@pytest.mark.parametrize(
    ("owner", "method_name"),
    [
        (urllib_request.OpenerDirector, "_open"),
        (urllib_request.OpenerDirector, "_call_chain"),
        (urllib_request.OpenerDirector, "error"),
        (urllib_request.HTTPSHandler, "https_open"),
        (urllib_request.HTTPSHandler, "https_request"),
        (urllib_request.AbstractHTTPHandler, "do_open"),
        (urllib_request.HTTPErrorProcessor, "https_response"),
    ],
)
def test_lower_urllib_class_rebind_revokes_provider_origin(
    monkeypatch: pytest.MonkeyPatch,
    owner: type[object],
    method_name: str,
) -> None:
    transport = UrllibBetfairHistoricalTransport()
    assert historical_module._canonical_historical_network_transport(transport) is True

    monkeypatch.setattr(owner, method_name, lambda *args, **kwargs: None)

    assert historical_module._canonical_historical_network_transport(transport) is False


def test_https_handler_instance_shadow_revokes_provider_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = UrllibBetfairHistoricalTransport()
    https_handler = next(
        handler
        for handler in transport._opener.handlers
        if type(handler) is urllib_request.HTTPSHandler
    )

    monkeypatch.setattr(
        https_handler,
        "https_open",
        lambda *args, **kwargs: None,
    )

    assert historical_module._canonical_historical_network_transport(transport) is False


def test_https_open_dispatch_substitution_revokes_provider_origin() -> None:
    transport = UrllibBetfairHistoricalTransport()
    transport._opener.handle_open["https"] = []

    assert historical_module._canonical_historical_network_transport(transport) is False


def test_https_response_dispatch_substitution_revokes_provider_origin() -> None:
    transport = UrllibBetfairHistoricalTransport()
    transport._opener.process_response["https"] = []

    assert historical_module._canonical_historical_network_transport(transport) is False


def test_redirect_dispatch_substitution_revokes_provider_origin() -> None:
    transport = UrllibBetfairHistoricalTransport()
    transport._opener.handle_error["http"][302] = []

    assert historical_module._canonical_historical_network_transport(transport) is False
