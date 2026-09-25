"""Seal the public Betfair placeOrders surface to product-owned execution truth.

The irreversible provider primitive is intentionally non-public.  The canonical
high-level ``execute_betfair_supervised_action`` path is the only product entrypoint
allowed to obtain it, after approval/ledger reservation and before its durable
STOP/SUBMITTED ordering.  Ordinary callers still see a narrow ``place_action``
signature for compatibility, but that surface is fail-closed and can never create
a provider effect.

This boundary also prevents caller substitution of transport/parser/clock seams.
Those deterministic seams remain available only to the captured private primitive
used by the exact canonical high-level execution code object.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import betfair_supervised_execution as _impl


_CLIENT_TYPE = _impl.BetfairSupervisedPlaceOrdersClient
_CANONICAL_TRANSPORT_TYPE = _impl.UrllibBetfairHttpTransport
_PRIVATE_PLACE_ACTION = _CLIENT_TYPE.__dict__.get("place_action")
_PRIVATE_PLACE_ACTION_CODE = getattr(_PRIVATE_PLACE_ACTION, "__code__", None)
_CANONICAL_EXECUTE = _impl.execute_betfair_supervised_action
_CANONICAL_EXECUTE_CODE = getattr(_CANONICAL_EXECUTE, "__code__", None)
_PROVIDER_HTTP_POST = _impl._CANONICAL_PROVIDER_HTTP_POST
_PROVIDER_HTTP_POST_CODE = _impl._CANONICAL_PROVIDER_HTTP_POST_CODE
_RESPONSE_PARSER = _impl._CANONICAL_PARSE_PLACE_ORDERS_RESPONSE
_RESPONSE_PARSER_CODE = _impl._CANONICAL_PARSE_PLACE_ORDERS_RESPONSE_CODE
_OBSERVATION_CLOCK = _impl._CANONICAL_PROVIDER_OBSERVATION_CLOCK
_OBSERVATION_CLOCK_CODE = _impl._CANONICAL_PROVIDER_OBSERVATION_CLOCK_CODE

if (
    not callable(_PRIVATE_PLACE_ACTION)
    or _PRIVATE_PLACE_ACTION_CODE is None
    or _impl._CANONICAL_BETFAIR_PLACE_ACTION is not _PRIVATE_PLACE_ACTION
    or _impl._CANONICAL_BETFAIR_PLACE_ACTION_CODE is not _PRIVATE_PLACE_ACTION_CODE
    or not callable(_CANONICAL_EXECUTE)
    or _CANONICAL_EXECUTE_CODE is None
    or not callable(_PROVIDER_HTTP_POST)
    or _PROVIDER_HTTP_POST_CODE is None
    or not callable(_RESPONSE_PARSER)
    or _RESPONSE_PARSER_CODE is None
    or not callable(_OBSERVATION_CLOCK)
    or _OBSERVATION_CLOCK_CODE is None
):
    raise RuntimeError("canonical Betfair provider-write dispatch is unavailable")


def _canonical_internal_dispatch_unchanged() -> bool:
    return (
        _CLIENT_TYPE is _impl.BetfairSupervisedPlaceOrdersClient
        and _CANONICAL_TRANSPORT_TYPE is _impl.UrllibBetfairHttpTransport
        and _CLIENT_TYPE.__dict__.get("place_action") is _BOUNDARY
        and _impl._CANONICAL_BETFAIR_PLACE_ACTION is _PRIVATE_PLACE_ACTION
        and _impl._CANONICAL_BETFAIR_PLACE_ACTION_CODE
        is _PRIVATE_PLACE_ACTION_CODE
        and getattr(_PRIVATE_PLACE_ACTION, "__code__", None)
        is _PRIVATE_PLACE_ACTION_CODE
        and _impl.execute_betfair_supervised_action is _CANONICAL_EXECUTE
        and getattr(_CANONICAL_EXECUTE, "__code__", None)
        is _CANONICAL_EXECUTE_CODE
        and _impl._PROVIDER_HTTP_POST is _PROVIDER_HTTP_POST
        and _impl._CANONICAL_PROVIDER_HTTP_POST is _PROVIDER_HTTP_POST
        and getattr(_PROVIDER_HTTP_POST, "__code__", None)
        is _PROVIDER_HTTP_POST_CODE
        and _impl._parse_place_orders_response is _RESPONSE_PARSER
        and _impl._CANONICAL_PARSE_PLACE_ORDERS_RESPONSE is _RESPONSE_PARSER
        and getattr(_RESPONSE_PARSER, "__code__", None)
        is _RESPONSE_PARSER_CODE
        and _impl._provider_observation_now is _OBSERVATION_CLOCK
        and _impl._CANONICAL_PROVIDER_OBSERVATION_CLOCK is _OBSERVATION_CLOCK
        and getattr(_OBSERVATION_CLOCK, "__code__", None)
        is _OBSERVATION_CLOCK_CODE
    )


def _public_place_action(
    self: _impl.BetfairSupervisedPlaceOrdersClient,
    action: _impl.ExecutionAction,
    *,
    profile: _impl.BookmakerCapabilityProfile,
    bound: _impl.BoundSupervisedExecutionPlan,
    provider_order_ref: str,
    execution_workspace: Path,
) -> _impl.BetfairPlaceExecutionReport:
    """Fail closed: provider effects require the canonical approval/ledger path."""

    raise _impl.BetfairSupervisedExecutionError(
        "direct public Betfair provider write is disabled; "
        "use execute_betfair_supervised_action"
    )


_public_place_action.__name__ = "place_action"
_public_place_action.__qualname__ = f"{_CLIENT_TYPE.__name__}.place_action"
_public_place_action.__module__ = _impl.__name__


class _PlaceActionBoundary:
    """Expose the private provider primitive only to the exact canonical executor."""

    __slots__ = ()

    def __get__(self, instance: object, owner: type | None = None):
        try:
            caller_code = sys._getframe(1).f_code
        except (AttributeError, ValueError):
            caller_code = None

        if caller_code is _CANONICAL_EXECUTE_CODE:
            if not _canonical_internal_dispatch_unchanged():
                raise _impl.BetfairSupervisedExecutionError(
                    "canonical Betfair internal provider-write authority changed"
                )
            dispatch = _PRIVATE_PLACE_ACTION
        else:
            dispatch = _public_place_action

        if instance is None:
            return dispatch
        return dispatch.__get__(instance, owner or _CLIENT_TYPE)


_BOUNDARY = _PlaceActionBoundary()
_CLIENT_TYPE.place_action = _BOUNDARY
