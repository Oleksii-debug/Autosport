"""Seal the public Betfair placeOrders surface to product-owned execution truth.

The irreversible provider primitive is intentionally non-public. The canonical
high-level ``execute_betfair_supervised_action`` path is the only product entrypoint
allowed to obtain it, after approval/ledger reservation and before its durable
STOP/SUBMITTED ordering. Ordinary callers still see a narrow ``place_action``
signature for compatibility, but that surface is fail-closed and can never create
a provider effect.

This boundary also composes the canonical #1891 trusted-runtime prerequisite without
creating another runtime/store/lock authority. The exact #1891 lock is held from a
fresh workspace-bound profile re-resolution through the private placeOrders call, so
runtime-profile revocation and the irreversible provider effect are linearized: a
revocation that wins first denies before SUBMITTED/provider I/O; a write admitted
first keeps the exact RUNNING profile current until the provider call exits.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import betfair_supervised_execution as _impl
from . import trusted_runtime_code_profile as _runtime_profile


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

# Reuse the exact canonical #1891 process-local authority graph. These are not new
# mirrors: identity checks below deliberately fail closed if the owning module
# replaces any part of the live profile registry/resolver after composition.
_TRUSTED_PROFILE_TYPE = _runtime_profile.TrustedRuntimeCodeProfile
_TRUSTED_PROFILE_ERROR = _runtime_profile.TrustedRuntimeCodeProfileError
_TRUSTED_PROFILE_LOCK = _runtime_profile._LOCK
_TRUSTED_ACTIVE_BY_WORKSPACE = _runtime_profile._ACTIVE_BY_WORKSPACE
_TRUSTED_ISSUED = _runtime_profile._ISSUED
_REQUIRE_TRUSTED_PROFILE = (
    _runtime_profile.require_authoritative_trusted_runtime_code_profile
)
_REQUIRE_TRUSTED_PROFILE_CODE = getattr(_REQUIRE_TRUSTED_PROFILE, "__code__", None)

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
    or type(_TRUSTED_ACTIVE_BY_WORKSPACE) is not dict
    or type(_TRUSTED_ISSUED) is not dict
    or not callable(_REQUIRE_TRUSTED_PROFILE)
    or _REQUIRE_TRUSTED_PROFILE_CODE is None
):
    raise RuntimeError("canonical Betfair provider-write composition is unavailable")


def _trusted_profile_graph_unchanged() -> bool:
    return (
        _runtime_profile.TrustedRuntimeCodeProfile is _TRUSTED_PROFILE_TYPE
        and _runtime_profile.TrustedRuntimeCodeProfileError is _TRUSTED_PROFILE_ERROR
        and _runtime_profile._LOCK is _TRUSTED_PROFILE_LOCK
        and _runtime_profile._ACTIVE_BY_WORKSPACE is _TRUSTED_ACTIVE_BY_WORKSPACE
        and _runtime_profile._ISSUED is _TRUSTED_ISSUED
        and _runtime_profile.require_authoritative_trusted_runtime_code_profile
        is _REQUIRE_TRUSTED_PROFILE
        and getattr(_REQUIRE_TRUSTED_PROFILE, "__code__", None)
        is _REQUIRE_TRUSTED_PROFILE_CODE
    )


def _canonical_workspace_text(value: object) -> str:
    try:
        resolved = Path(value).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted runtime profile requires canonical execution workspace"
        ) from exc
    text = str(resolved)
    if not text or text.strip() != text:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted runtime profile workspace is not canonical"
        )
    return text


def _require_current_workspace_profile_locked(workspace: str):
    """Resolve the sole exact #1891 active issuance while its own lock is held."""

    if not _trusted_profile_graph_unchanged():
        raise _impl.BetfairSupervisedExecutionError(
            "trusted runtime profile authority changed"
        )
    profile_identity = _TRUSTED_ACTIVE_BY_WORKSPACE.get(workspace)
    if type(profile_identity) is not int:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted RUNNING product runtime profile is required for provider write"
        )
    record = _TRUSTED_ISSUED.get(profile_identity)
    profile = getattr(record, "profile", None)
    if type(profile) is not _TRUSTED_PROFILE_TYPE or id(profile) != profile_identity:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted runtime profile issuance is inconsistent"
        )
    try:
        resolved = _REQUIRE_TRUSTED_PROFILE(profile, workspace=workspace)
    except _TRUSTED_PROFILE_ERROR as exc:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted RUNNING product runtime profile is not authoritative"
        ) from exc
    if resolved is not profile:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted runtime profile resolver changed exact issuance"
        )
    return profile


def _trusted_private_place_action(
    self: _impl.BetfairSupervisedPlaceOrdersClient,
    action: _impl.ExecutionAction,
    *,
    profile: _impl.BookmakerCapabilityProfile,
    bound: _impl.BoundSupervisedExecutionPlan,
    provider_order_ref: str,
    execution_workspace: Path,
    _before_transport=None,
    _transport_post=None,
    _response_parser=None,
    _observation_clock=None,
):
    """Compose current trusted-runtime authority with the exact private write seam."""

    try:
        caller_code = sys._getframe(1).f_code
    except (AttributeError, ValueError):
        caller_code = None
    if caller_code is not _CANONICAL_EXECUTE_CODE:
        raise _impl.BetfairSupervisedExecutionError(
            "private Betfair provider write requires canonical execution caller"
        )
    if (
        _transport_post is not _PROVIDER_HTTP_POST
        or _response_parser is not _RESPONSE_PARSER
        or _observation_clock is not _OBSERVATION_CLOCK
        or not callable(_before_transport)
    ):
        raise _impl.BetfairSupervisedExecutionError(
            "canonical Betfair internal provider-write dependencies changed"
        )
    workspace = _canonical_workspace_text(execution_workspace)

    # This is deliberately the existing #1891 RLock, not a new admission lock.
    # Holding it through the private call prevents STOP/runtime cleanup from
    # revoking the profile between positive re-resolution and irreversible POST.
    with _TRUSTED_PROFILE_LOCK:
        _require_current_workspace_profile_locked(workspace)
        if not _canonical_internal_dispatch_unchanged():
            raise _impl.BetfairSupervisedExecutionError(
                "canonical Betfair internal provider-write authority changed"
            )
        return _PRIVATE_PLACE_ACTION(
            self,
            action,
            profile=profile,
            bound=bound,
            provider_order_ref=provider_order_ref,
            execution_workspace=execution_workspace,
            _before_transport=_before_transport,
            _transport_post=_PROVIDER_HTTP_POST,
            _response_parser=_RESPONSE_PARSER,
            _observation_clock=_OBSERVATION_CLOCK,
        )


_TRUSTED_PRIVATE_PLACE_ACTION = _trusted_private_place_action
_TRUSTED_PRIVATE_PLACE_ACTION_CODE = _TRUSTED_PRIVATE_PLACE_ACTION.__code__

# The implementation's high-level executor calls this captured module global. Point
# it at the composed wrapper before installing the public descriptor; the original
# private primitive remains reachable only as the wrapper's captured dependency.
_impl._CANONICAL_BETFAIR_PLACE_ACTION = _TRUSTED_PRIVATE_PLACE_ACTION
_impl._CANONICAL_BETFAIR_PLACE_ACTION_CODE = _TRUSTED_PRIVATE_PLACE_ACTION_CODE


def _canonical_internal_dispatch_unchanged() -> bool:
    return (
        _CLIENT_TYPE is _impl.BetfairSupervisedPlaceOrdersClient
        and _CANONICAL_TRANSPORT_TYPE is _impl.UrllibBetfairHttpTransport
        and _CLIENT_TYPE.__dict__.get("place_action") is _BOUNDARY
        and _impl._CANONICAL_BETFAIR_PLACE_ACTION is _TRUSTED_PRIVATE_PLACE_ACTION
        and _impl._CANONICAL_BETFAIR_PLACE_ACTION_CODE
        is _TRUSTED_PRIVATE_PLACE_ACTION_CODE
        and getattr(_TRUSTED_PRIVATE_PLACE_ACTION, "__code__", None)
        is _TRUSTED_PRIVATE_PLACE_ACTION_CODE
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
        and _trusted_profile_graph_unchanged()
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
    """Expose the composed private provider primitive only to the exact executor."""

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
            dispatch = _TRUSTED_PRIVATE_PLACE_ACTION
        else:
            dispatch = _public_place_action

        if instance is None:
            return dispatch
        return dispatch.__get__(instance, owner or _CLIENT_TYPE)


_BOUNDARY = _PlaceActionBoundary()
_CLIENT_TYPE.place_action = _BOUNDARY
