from __future__ import annotations

from pathlib import Path

import pytest

import autosport._betfair_supervised_public_transport_boundary as _boundary

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



def test_raw_effectful_place_action_is_not_module_addressable() -> None:
    assert "_PRIVATE_PLACE_ACTION" not in vars(_boundary)
    assert "_RAW_PLACE_ACTION" not in vars(_boundary)
    assert "_RAW_PLACE_ACTION_CODE" not in vars(_boundary)


def test_trusted_private_wrapper_rejects_direct_module_call() -> None:
    client = _client()

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="private Betfair provider write requires canonical execution caller",
    ):
        _boundary._TRUSTED_PRIVATE_PLACE_ACTION(
            client,
            object(),
            profile=object(),
            bound=object(),
            provider_order_ref="0" * 32,
            execution_workspace=Path("."),
            _before_transport=lambda: None,
            _transport_post=_boundary._PROVIDER_HTTP_POST,
            _response_parser=_boundary._RESPONSE_PARSER,
            _observation_clock=_boundary._OBSERVATION_CLOCK,
        )
