"""Seal the public Betfair placeOrders surface to product-owned transport truth.

The canonical provider-write implementation still needs one private pre-transport
callback so ``execute_betfair_supervised_action`` can cross the durable SUBMITTED
boundary after STOP admission and immediately before the irreversible POST. That
private seam must not be caller authority.

This composition guard therefore keeps the already-qualified canonical private
primitive untouched. A caller-code-sensitive descriptor returns that primitive only
to the exact canonical high-level execution code object. Every ordinary class or
instance access receives a genuinely narrow public function whose Python signature
contains only product inputs and whose transport/parser/clock are product-owned.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import betfair_supervised_execution as _impl


_CLIENT_TYPE = _impl.BetfairSupervisedPlaceOrdersClient
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


def _canonical_public_dispatch_unchanged() -> bool:
    return (
        _CLIENT_TYPE is _impl.BetfairSupervisedPlaceOrdersClient
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
    """Place one bounded action through product-owned provider truth only."""

    if type(self) is not _CLIENT_TYPE:
        raise _impl.BetfairSupervisedExecutionError(
            "public Betfair provider write requires the exact canonical client"
        )
    if not _canonical_public_dispatch_unchanged():
        raise _impl.BetfairSupervisedExecutionError(
            "canonical Betfair public provider-write authority changed"
        )
    return _PRIVATE_PLACE_ACTION(
        self,
        action,
        profile=profile,
        bound=bound,
        provider_order_ref=provider_order_ref,
        execution_workspace=execution_workspace,
        _transport_post=_PROVIDER_HTTP_POST,
        _response_parser=_RESPONSE_PARSER,
        _observation_clock=_OBSERVATION_CLOCK,
    )


_public_place_action.__name__ = "place_action"
_public_place_action.__qualname__ = f"{_CLIENT_TYPE.__name__}.place_action"
_public_place_action.__module__ = _impl.__name__


class _PlaceActionBoundary:
    """Expose public or private dispatch according to exact caller code authority."""

    __slots__ = ()

    def __get__(self, instance: object, owner: type | None = None):
        try:
            caller_code = sys._getframe(1).f_code
        except (AttributeError, ValueError):
            caller_code = None
        dispatch = (
            _PRIVATE_PLACE_ACTION
            if caller_code is _CANONICAL_EXECUTE_CODE
            else _public_place_action
        )
        if instance is None:
            return dispatch
        return dispatch.__get__(instance, owner or _CLIENT_TYPE)


_BOUNDARY = _PlaceActionBoundary()
_CLIENT_TYPE.place_action = _BOUNDARY
