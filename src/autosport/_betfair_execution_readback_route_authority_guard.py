"""Fail closed when Betfair readback scope changes during one provider capture.

The native readback surface can produce structurally useful captures for custom test
transports that deliberately carry no K07 product-origin authority. That structural
mode still must not combine one provider I/O sequence across different configured
venue/account routes or authenticated session credentials while the capture is in
flight.

This guard wraps the already-composed readback callable and preserves all existing
origin/timeout semantics. It adds no provider write or execution authority.
"""

from __future__ import annotations

from . import betfair_account_readonly as _adapter


_ERROR = _adapter.BetfairReadOnlyError
_CLIENT_TYPE = _adapter.BetfairReadOnlyClient
_CREDENTIAL_TYPE = _adapter.BetfairSessionCredentials
_ENVELOPE_TYPE = _adapter.BetfairExecutionReadbackEnvelope
_ORIGINAL_READ = _CLIENT_TYPE.read_execution_readback
_ORIGINAL_READ_CODE = getattr(_ORIGINAL_READ, "__code__", None)

if _ORIGINAL_READ_CODE is None:
    raise RuntimeError("Betfair execution-readback route authority is unavailable")


def _install_route_authority_guard() -> None:
    error_type = _ERROR
    client_type = _CLIENT_TYPE
    credential_type = _CREDENTIAL_TYPE
    envelope_type = _ENVELOPE_TYPE
    original_read = _ORIGINAL_READ
    original_read_code = _ORIGINAL_READ_CODE

    def _scope_snapshot(client) -> tuple[str, str, object, str, str]:
        try:
            state = object.__getattribute__(client, "__dict__")
            venue_id = state["_venue_id"]
            account_id = state["_account_id"]
            credentials = state["_credentials"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise error_type(
                "configured account route changed during execution readback"
            ) from exc
        if (
            type(venue_id) is not str
            or not venue_id
            or venue_id != venue_id.strip()
            or type(account_id) is not str
            or not account_id
            or account_id != account_id.strip()
        ):
            raise error_type(
                "configured account route changed during execution readback"
            )
        if type(credentials) is not credential_type:
            raise error_type(
                "configured credentials changed during execution readback"
            )
        try:
            application_key = object.__getattribute__(credentials, "application_key")
            session_token = object.__getattribute__(credentials, "session_token")
        except AttributeError as exc:
            raise error_type(
                "configured credentials changed during execution readback"
            ) from exc
        if (
            type(application_key) is not str
            or not application_key
            or application_key != application_key.strip()
            or type(session_token) is not str
            or not session_token
            or session_token != session_token.strip()
        ):
            raise error_type(
                "configured credentials changed during execution readback"
            )
        return venue_id, account_id, credentials, application_key, session_token

    def guarded_read(self, *args, **kwargs):
        if (
            client_type.read_execution_readback is not guarded_read
            or getattr(original_read, "__code__", None) is not original_read_code
        ):
            raise error_type("execution readback route authority implementation changed")
        (
            expected_venue,
            expected_account,
            expected_credentials,
            expected_application_key,
            expected_session_token,
        ) = _scope_snapshot(self)
        capture = original_read(self, *args, **kwargs)
        (
            current_venue,
            current_account,
            current_credentials,
            current_application_key,
            current_session_token,
        ) = _scope_snapshot(self)
        if (
            current_credentials is not expected_credentials
            or current_application_key != expected_application_key
            or current_session_token != expected_session_token
        ):
            raise error_type(
                "configured credentials changed during execution readback"
            )
        if (
            current_venue != expected_venue
            or current_account != expected_account
            or type(capture) is not envelope_type
            or capture.venue_id != expected_venue
            or capture.account_id != expected_account
        ):
            raise error_type(
                "configured account route changed during execution readback"
            )
        if (
            client_type.read_execution_readback is not guarded_read
            or getattr(original_read, "__code__", None) is not original_read_code
        ):
            raise error_type("execution readback route authority implementation changed")
        return capture

    if client_type.read_execution_readback is not original_read:
        raise RuntimeError("Betfair execution readback changed before route guard")
    client_type.read_execution_readback = guarded_read


_install_route_authority_guard()
del _install_route_authority_guard
