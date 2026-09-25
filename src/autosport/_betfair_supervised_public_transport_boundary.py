"""Seal the public Betfair placeOrders surface to product-owned transport truth.

The canonical provider-write implementation still needs one private pre-transport
callback so ``execute_betfair_supervised_action`` can cross the durable SUBMITTED
boundary after STOP admission and immediately before the irreversible POST.  That
private seam must not be caller authority.

This composition guard preserves the already-qualified provider-write primitive and
its mid-flight code-identity falsifiers, but replaces the public method surface with
a narrow signature.  Hidden transport/parser/clock arguments are accepted only when
the immediate caller is the exact canonical high-level execution code object, and
only when those arguments are the exact product-owned helpers captured below.
Ordinary public calls always dispatch through the product-owned HTTPS POST, response
parser, and observation clock; constructor-injected transport/clock objects therefore
cannot mint terminal provider truth through the public method.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Callable

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
        and _CLIENT_TYPE.__dict__.get("place_action") is place_action
        and _impl._CANONICAL_BETFAIR_PLACE_ACTION is place_action
        and _impl._CANONICAL_BETFAIR_PLACE_ACTION_CODE is place_action.__code__
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


def place_action(
    self: _impl.BetfairSupervisedPlaceOrdersClient,
    action: _impl.ExecutionAction,
    *,
    profile: _impl.BookmakerCapabilityProfile,
    bound: _impl.BoundSupervisedExecutionPlan,
    provider_order_ref: str,
    execution_workspace: Path,
    _before_transport: Callable[[], None] | None = None,
    _transport_post: Callable[..., bytes] | None = None,
    _response_parser: Callable[..., _impl.BetfairPlaceExecutionReport] | None = None,
    _observation_clock: Callable[[], str] | None = None,
) -> _impl.BetfairPlaceExecutionReport:
    """Place one bounded action without exposing provider-truth injection authority."""

    if type(self) is not _CLIENT_TYPE:
        raise _impl.BetfairSupervisedExecutionError(
            "public Betfair provider write requires the exact canonical client"
        )
    if not _canonical_public_dispatch_unchanged():
        raise _impl.BetfairSupervisedExecutionError(
            "canonical Betfair public provider-write authority changed"
        )

    private_requested = any(
        value is not None
        for value in (
            _before_transport,
            _transport_post,
            _response_parser,
            _observation_clock,
        )
    )
    if private_requested:
        try:
            caller_code = sys._getframe(1).f_code
        except (AttributeError, ValueError) as exc:
            raise _impl.BetfairSupervisedExecutionError(
                "private Betfair provider-write dispatch caller is unavailable"
            ) from exc
        if caller_code is not _CANONICAL_EXECUTE_CODE:
            raise _impl.BetfairSupervisedExecutionError(
                "private Betfair provider-write dispatch is internal-only"
            )
        if (
            _before_transport is None
            or not callable(_before_transport)
            or _transport_post is not _PROVIDER_HTTP_POST
            or _response_parser is not _RESPONSE_PARSER
            or _observation_clock is not _OBSERVATION_CLOCK
        ):
            raise _impl.BetfairSupervisedExecutionError(
                "private Betfair provider-write dispatch requires canonical helpers"
            )
    else:
        _transport_post = _PROVIDER_HTTP_POST
        _response_parser = _RESPONSE_PARSER
        _observation_clock = _OBSERVATION_CLOCK

    return _PRIVATE_PLACE_ACTION(
        self,
        action,
        profile=profile,
        bound=bound,
        provider_order_ref=provider_order_ref,
        execution_workspace=execution_workspace,
        _before_transport=_before_transport,
        _transport_post=_transport_post,
        _response_parser=_response_parser,
        _observation_clock=_observation_clock,
    )


# The runtime function deliberately retains the private parameters so the canonical
# high-level implementation can make one direct code-authorized call.  Public
# introspection and ordinary bound-method use expose only the supported product API.
place_action.__signature__ = inspect.Signature(
    parameters=(
        inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("action", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("profile", inspect.Parameter.KEYWORD_ONLY),
        inspect.Parameter("bound", inspect.Parameter.KEYWORD_ONLY),
        inspect.Parameter("provider_order_ref", inspect.Parameter.KEYWORD_ONLY),
        inspect.Parameter("execution_workspace", inspect.Parameter.KEYWORD_ONLY),
    ),
    return_annotation=_impl.BetfairPlaceExecutionReport,
)
place_action.__module__ = _impl.__name__
place_action.__qualname__ = f"{_CLIENT_TYPE.__name__}.place_action"

_CLIENT_TYPE.place_action = place_action
_impl._CANONICAL_BETFAIR_PLACE_ACTION = place_action
_impl._CANONICAL_BETFAIR_PLACE_ACTION_CODE = place_action.__code__
