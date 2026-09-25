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

    with pytest.raises(TypeError, match="_transport_post"):
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

    with pytest.raises(TypeError, match="_before_transport"):
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


def test_direct_public_call_is_not_provider_effect_authority() -> None:
    """Only the approval/ledger executor may obtain the private provider primitive."""

    client = _client()

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="direct public Betfair provider write is disabled",
    ):
        client.place_action(
            object(),
            profile=object(),
            bound=object(),
            provider_order_ref="0" * 32,
            execution_workspace=Path("."),
        )


def test_direct_public_class_call_is_also_fail_closed() -> None:
    """Descriptor access through the class must not expose the private primitive."""

    client = _client()

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="direct public Betfair provider write is disabled",
    ):
        BetfairSupervisedPlaceOrdersClient.place_action(
            client,
            object(),
            profile=object(),
            bound=object(),
            provider_order_ref="0" * 32,
            execution_workspace=Path("."),
        )


def test_public_write_with_caller_transport_instance_still_cannot_write() -> None:
    class ForgedTransport:
        pass

    client = BetfairSupervisedPlaceOrdersClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=ForgedTransport(),
    )

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="direct public Betfair provider write is disabled",
    ):
        client.place_action(
            object(),
            profile=object(),
            bound=object(),
            provider_order_ref="0" * 32,
            execution_workspace=Path("."),
        )
