"""Plan 4 Section 4: hostile echoed Betfair selection identities, offline only.

The external JSON response must not reinterpret a different selection identity
as the originally submitted integer. No account or provider transport is used.
"""
from __future__ import annotations

from hashlib import sha256
import json

import pytest

import test_betfair_supervised_execution as fixtures
from autosport.betfair_supervised_execution import (
    BetfairPlaceOrdersAmbiguous,
    _canonical_place_orders_request_body,
    _parse_place_orders_response,
)


def _parsed_response(*, selection_id: str, echoed_selection: object):
    profile = fixtures._profile()
    bound, _approval, _goal = fixtures._bound(
        profile, selection_id=selection_id
    )
    action = bound.execution_plan.actions[0]
    request_bytes = _canonical_place_orders_request_body(
        action, provider_order_ref="a" * 32, request_id=7
    )
    request = json.loads(request_bytes.decode("utf-8"))
    response = json.loads(
        fixtures._response(
            request,
            matched=action.requested_stake,
            average=action.requested_odds,
        ).decode("utf-8")
    )
    response["result"]["instructionReports"][0]["instruction"][
        "selectionId"
    ] = echoed_selection
    payload = json.dumps(
        response, ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    return _parse_place_orders_response(
        payload,
        request_id=7,
        request_sha256=sha256(request_bytes).hexdigest(),
        action=action,
        provider_order_ref="a" * 32,
        observed_at=fixtures.READBACK_AT,
    )


@pytest.mark.parametrize(
    ("selection_id", "echoed_selection"),
    [
        ("42", 42.75),  # Decimal('42.75') decoded -> int() used to truncate to 42.
        ("42", 42.0),   # Decimal('42.0') must not impersonate integer 42.
        ("42", "42"),  # String must not impersonate integral JSON number.
        ("1", True),   # bool is a subclass of int but not a selection ID.
        ("42", None),
        ("42", -42),
    ],
)
def test_noninteger_or_invalid_echo_never_verifies_selection_identity(
    selection_id: str, echoed_selection: object
) -> None:
    with pytest.raises(
        BetfairPlaceOrdersAmbiguous,
        match="echoed selection is malformed",
    ):
        _parsed_response(
            selection_id=selection_id, echoed_selection=echoed_selection
        )


def test_exact_integral_json_echo_is_accepted() -> None:
    report = _parsed_response(selection_id="42", echoed_selection=42)
    assert report.instruction.size_matched > 0
    assert report.action_id
    assert report.request_id == 7
