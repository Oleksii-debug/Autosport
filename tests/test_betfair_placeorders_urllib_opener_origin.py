from __future__ import annotations

import urllib.request as urllib_request

import autosport.betfair_supervised_execution as betfair_supervised_execution
from autosport.betfair_account_readonly import UrllibBetfairHttpTransport


class _ForgedProcessOpener:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def open(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("process-global opener must never serve provider writes")


def test_process_global_urllib_opener_is_outside_terminal_provider_authority(
    monkeypatch,
) -> None:
    """An ambient urllib opener cannot replace the product-owned private opener."""

    forged_opener = _ForgedProcessOpener()
    private_opener = (
        betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER
    )
    private_handlers = (
        betfair_supervised_execution
        ._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER_HANDLERS
    )
    private_dispatch = (
        betfair_supervised_execution
        ._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER_DISPATCH
    )
    private_methods = (
        betfair_supervised_execution
        ._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER_METHODS
    )
    graph_matches = (
        betfair_supervised_execution
        ._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER_GRAPH_MATCHES
    )

    assert "urlopen" not in UrllibBetfairHttpTransport.post.__globals__
    assert (
        UrllibBetfairHttpTransport.post.__globals__["build_opener"]
        is betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_BUILD_OPENER
    )
    assert private_opener is not getattr(urllib_request, "_opener", None)

    monkeypatch.setattr(urllib_request, "_opener", forged_opener)

    assert urllib_request._opener is forged_opener
    assert (
        betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER
        is private_opener
    )
    assert graph_matches(
        private_opener,
        private_handlers,
        private_dispatch,
        private_methods,
    )
    assert forged_opener.calls == []
