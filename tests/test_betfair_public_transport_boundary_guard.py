from __future__ import annotations

from pathlib import Path

import pytest

from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionError,
    BetfairSupervisedPlaceOrdersClient,
)


def _client() -> BetfairSupervisedPlaceOrdersClient:
    return BetfairSupervisedPlaceOrdersClient(
        BetfairSessionCredentials("app-key", "session-token")
    )


def test_direct_class_call_cannot_inject_transport_override() -> None:
    client = _client()
    forged_calls: list[object] = []

    def forged_transport(*args, **kwargs):
        forged_calls.append((args, kwargs))
        return b"{}"

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="private Betfair provider-write dispatch is internal-only",
    ):
        BetfairSupervisedPlaceOrdersClient.place_action(
            client,
            object(),
            profile=object(),
            bound=object(),
            provider_order_ref="0" * 32,
            execution_workspace=Path("."),
            _transport_post=forged_transport,
        )

    assert forged_calls == []


def test_direct_class_call_cannot_inject_pre_transport_callback() -> None:
    client = _client()
    callback_calls: list[str] = []

    def forged_before_transport() -> None:
        callback_calls.append("called")

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="private Betfair provider-write dispatch is internal-only",
    ):
        BetfairSupervisedPlaceOrdersClient.place_action(
            client,
            object(),
            profile=object(),
            bound=object(),
            provider_order_ref="0" * 32,
            execution_workspace=Path("."),
            _before_transport=forged_before_transport,
        )

    assert callback_calls == []
