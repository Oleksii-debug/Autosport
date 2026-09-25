from __future__ import annotations

import inspect

from autosport.betfair_supervised_execution import BetfairSupervisedPlaceOrdersClient


def test_public_place_action_does_not_accept_transport_authority_overrides() -> None:
    """Production callers must not be able to inject provider truth at the public API."""

    signature = inspect.signature(BetfairSupervisedPlaceOrdersClient.place_action)
    forbidden = {
        "_before_transport",
        "_transport_post",
        "_response_parser",
        "_observation_clock",
    }

    assert forbidden.isdisjoint(signature.parameters)
    assert all(
        parameter.kind is not inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
