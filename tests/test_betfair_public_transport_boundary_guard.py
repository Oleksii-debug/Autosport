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


def test_direct_public_call_enters_canonical_action_validation() -> None:
    """The narrow public surface delegates; it is not an unconditional deny shim."""

    client = _client()

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="action must be canonical ExecutionAction",
    ):
        client.place_action(
            object(),
            profile=object(),
            bound=object(),
            provider_order_ref="0" * 32,
            execution_workspace=Path("."),
        )


def test_public_write_rejects_caller_transport_instance() -> None:
    class ForgedTransport:
        pass

    client = BetfairSupervisedPlaceOrdersClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=ForgedTransport(),
    )

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="requires canonical HTTPS transport state",
    ):
        client.place_action(
            object(),
            profile=object(),
            bound=object(),
            provider_order_ref="0" * 32,
            execution_workspace=Path("."),
        )
