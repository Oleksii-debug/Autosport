"""Bind #1272 currency-qualified settlement profit to K07 auth origin.

The provider-row bridge can prove that account details and execution readback were
performed on the same Python client object, but mutable client credentials mean object
identity and public venue/account labels are not authenticated-origin authority.

K07 already owns that authority for the canonical Betfair client.  This composition
guard therefore requires one current K07 session context around the existing currency
bridge and keeps that context live through settlement ingestion.  It does not add a
provider client, transport, credential store, or write capability.
"""

from __future__ import annotations

from threading import Lock
from weakref import ref

from . import _betfair_settlement_provider_row_semantics as _semantics
from . import betfair_account_identity as _identity
from . import betfair_account_readonly as _adapter
from . import betfair_settlement_revisions as _settlement


_ERROR = _settlement.BetfairSettlementRevisionError
_IDENTITY_ERROR = _identity.BetfairAccountIdentityError
_CLIENT_TYPE = _adapter.BetfairReadOnlyClient
_CAPTURE_TYPE = _adapter.BetfairExecutionReadbackEnvelope
_IDENTITY_TYPE = _identity.BetfairAuthenticatedAccountIdentity
_ORIGINAL_QUALIFIED_READ = _semantics.read_currency_qualified_execution_readback
_ORIGINAL_CURRENCY_FOR_CAPTURE = _semantics._currency_for_capture
_RESOLVE_IDENTITY = _identity.resolve_betfair_authenticated_account_identity
_REQUIRE_IDENTITY = _identity.require_authoritative_betfair_account_identity

if (
    not callable(_ORIGINAL_QUALIFIED_READ)
    or not callable(_ORIGINAL_CURRENCY_FOR_CAPTURE)
    or not callable(_RESOLVE_IDENTITY)
    or not callable(_REQUIRE_IDENTITY)
):
    raise RuntimeError("Betfair settlement credential-origin authority is unavailable")


def _install_credential_origin_guard():
    issued: dict[
        int,
        tuple[object, object, _IDENTITY_TYPE, str, str, str, str],
    ] = {}
    lock = Lock()

    error_type = _ERROR
    identity_error_type = _IDENTITY_ERROR
    client_type = _CLIENT_TYPE
    capture_type = _CAPTURE_TYPE
    identity_type = _IDENTITY_TYPE
    original_qualified_read = _ORIGINAL_QUALIFIED_READ
    original_currency_for_capture = _ORIGINAL_CURRENCY_FOR_CAPTURE
    resolve_identity = _RESOLVE_IDENTITY
    require_identity = _REQUIRE_IDENTITY

    def _metadata_snapshot(function):
        code = getattr(function, "__code__", None)
        defaults = getattr(function, "__defaults__", None)
        kwdefaults = getattr(function, "__kwdefaults__", None)
        closure = getattr(function, "__closure__", None)
        if code is None:
            raise RuntimeError(
                "Betfair settlement credential-origin executable authority is unavailable"
            )
        kwitems = tuple(kwdefaults.items()) if kwdefaults is not None else ()
        try:
            closure_values = (
                tuple(cell.cell_contents for cell in closure)
                if closure is not None
                else ()
            )
        except ValueError as exc:
            raise RuntimeError(
                "Betfair settlement credential-origin closure authority is unavailable"
            ) from exc
        return code, defaults, kwdefaults, kwitems, closure, closure_values

    (
        resolve_identity_code,
        resolve_identity_defaults,
        resolve_identity_kwdefaults,
        resolve_identity_kwitems,
        resolve_identity_closure,
        resolve_identity_closure_values,
    ) = _metadata_snapshot(resolve_identity)
    (
        require_identity_code,
        require_identity_defaults,
        require_identity_kwdefaults,
        require_identity_kwitems,
        require_identity_closure,
        require_identity_closure_values,
    ) = _metadata_snapshot(require_identity)
    (
        qualified_read_code,
        qualified_read_defaults,
        qualified_read_kwdefaults,
        qualified_read_kwitems,
        qualified_read_closure,
        qualified_read_closure_values,
    ) = _metadata_snapshot(original_qualified_read)
    (
        currency_lookup_code,
        currency_lookup_defaults,
        currency_lookup_kwdefaults,
        currency_lookup_kwitems,
        currency_lookup_closure,
        currency_lookup_closure_values,
    ) = _metadata_snapshot(original_currency_for_capture)

    def _metadata_current(
        function,
        *,
        code,
        defaults,
        kwdefaults,
        kwitems,
        closure,
        closure_values,
    ) -> bool:
        current_kwdefaults = getattr(function, "__kwdefaults__", None)
        current_closure = getattr(function, "__closure__", None)
        try:
            current_closure_values = (
                tuple(cell.cell_contents for cell in current_closure)
                if current_closure is not None
                else ()
            )
        except ValueError:
            return False
        return (
            getattr(function, "__code__", None) is code
            and getattr(function, "__defaults__", None) is defaults
            and current_kwdefaults is kwdefaults
            and current_closure is closure
            and len(current_closure_values) == len(closure_values)
            and all(
                current is expected
                for current, expected in zip(
                    current_closure_values,
                    closure_values,
                    strict=True,
                )
            )
            and (
                current_kwdefaults is None
                or (
                    len(current_kwdefaults) == len(kwitems)
                    and all(
                        key in current_kwdefaults
                        and current_kwdefaults[key] is value
                        for key, value in kwitems
                    )
                )
            )
        )

    def _identity_verifier_current() -> bool:
        return (
            globals().get("_RESOLVE_IDENTITY") is resolve_identity
            and globals().get("_REQUIRE_IDENTITY") is require_identity
            and _metadata_current(
                resolve_identity,
                code=resolve_identity_code,
                defaults=resolve_identity_defaults,
                kwdefaults=resolve_identity_kwdefaults,
                kwitems=resolve_identity_kwitems,
                closure=resolve_identity_closure,
                closure_values=resolve_identity_closure_values,
            )
            and _metadata_current(
                require_identity,
                code=require_identity_code,
                defaults=require_identity_defaults,
                kwdefaults=require_identity_kwdefaults,
                kwitems=require_identity_kwitems,
                closure=require_identity_closure,
                closure_values=require_identity_closure_values,
            )
        )

    def _qualified_read_current() -> bool:
        return (
            globals().get("_ORIGINAL_QUALIFIED_READ") is original_qualified_read
            and _metadata_current(
                original_qualified_read,
                code=qualified_read_code,
                defaults=qualified_read_defaults,
                kwdefaults=qualified_read_kwdefaults,
                kwitems=qualified_read_kwitems,
                closure=qualified_read_closure,
                closure_values=qualified_read_closure_values,
            )
        )

    def _currency_lookup_current() -> bool:
        return (
            globals().get("_ORIGINAL_CURRENCY_FOR_CAPTURE")
            is original_currency_for_capture
            and _metadata_current(
                original_currency_for_capture,
                code=currency_lookup_code,
                defaults=currency_lookup_defaults,
                kwdefaults=currency_lookup_kwdefaults,
                kwitems=currency_lookup_kwitems,
                closure=currency_lookup_closure,
                closure_values=currency_lookup_closure_values,
            )
        )

    def _assert_identity_verifier_current() -> None:
        if not _identity_verifier_current():
            raise error_type(
                "settlement currency authenticated identity verifier authority changed"
            )

    def _assert_qualified_read_current() -> None:
        if not _qualified_read_current():
            raise error_type("settlement currency qualified read authority changed")

    def _assert_currency_lookup_current() -> None:
        if not _currency_lookup_current():
            raise error_type("settlement currency evidence lookup authority changed")

    def _require_k07(client, *, stage: str) -> _IDENTITY_TYPE:
        _assert_identity_verifier_current()
        try:
            identity = resolve_identity(client)
            _assert_identity_verifier_current()
            current = require_identity(identity, client=client)
            _assert_identity_verifier_current()
            return current
        except identity_error_type as exc:
            raise error_type(
                f"settlement currency {stage} lacks K07 authenticated client/session authority"
            ) from exc

    def _routing_snapshot(client) -> tuple[str, str]:
        try:
            state = vars(client)
        except TypeError as exc:
            raise error_type("settlement currency client state is unavailable") from exc
        venue_id = state.get("_venue_id")
        account_id = state.get("_account_id")
        if (
            type(venue_id) is not str
            or not venue_id
            or venue_id != venue_id.strip()
            or type(account_id) is not str
            or not account_id
            or account_id != account_id.strip()
        ):
            raise error_type("settlement currency routing identity is unavailable")
        return venue_id, account_id

    def _forget_capture(capture_id: int):
        def forget(_dead) -> None:
            with lock:
                issued.pop(capture_id, None)

        return forget

    def read_currency_qualified_execution_readback(
        client,
        *,
        action_id: str,
        market_id: str,
        provider_order_ref: str | None = None,
        page_size: int = 200,
    ):
        if type(client) is not client_type:
            raise error_type(
                "settlement currency acquisition requires exact BetfairReadOnlyClient"
            )

        expected_venue, expected_account = _routing_snapshot(client)
        before = _require_k07(client, stage="acquisition")
        _assert_qualified_read_current()
        capture = original_qualified_read(
            client,
            action_id=action_id,
            market_id=market_id,
            provider_order_ref=provider_order_ref,
            page_size=page_size,
        )
        _assert_qualified_read_current()
        if type(capture) is not capture_type:
            raise error_type("settlement readback is not canonical Betfair execution capture")

        _assert_identity_verifier_current()
        try:
            require_identity(before, client=client)
        except identity_error_type as exc:
            raise error_type(
                "settlement currency authenticated session changed during execution readback"
            ) from exc
        _assert_identity_verifier_current()

        current_venue, current_account = _routing_snapshot(client)
        if (
            current_venue != expected_venue
            or current_account != expected_account
            or capture.venue_id != expected_venue
            or capture.account_id != expected_account
        ):
            raise error_type("settlement currency routing identity changed during readback")

        after = _require_k07(client, stage="post-readback verification")
        if (
            before.session_context_id != after.session_context_id
            or before.venue_id != after.venue_id
            or before.currency_code != after.currency_code
        ):
            raise error_type(
                "settlement currency/readback authenticated session context changed"
            )

        _assert_currency_lookup_current()
        legacy_currency = original_currency_for_capture(capture)
        _assert_currency_lookup_current()
        if legacy_currency is None or legacy_currency != before.currency_code:
            raise error_type(
                "settlement currency evidence does not match K07 authenticated context"
            )

        capture_id = id(capture)
        capture_ref = ref(capture, _forget_capture(capture_id))
        client_ref = ref(client)
        with lock:
            issued[capture_id] = (
                capture_ref,
                client_ref,
                after,
                after.session_context_id,
                after.currency_code,
                expected_venue,
                expected_account,
            )
        return capture

    def currency_for_capture(capture) -> str | None:
        if type(capture) is not capture_type:
            return None
        with lock:
            record = issued.get(id(capture))
        if record is None or record[0]() is not capture:
            return None

        client = record[1]()
        if client is None:
            raise error_type(
                "settlement currency authenticated client expired before persistence"
            )
        identity = record[2]
        expected_context = record[3]
        expected_currency = record[4]
        expected_venue = record[5]
        expected_account = record[6]
        _assert_identity_verifier_current()
        try:
            current = require_identity(identity, client=client)
        except identity_error_type as exc:
            raise error_type(
                "settlement currency authenticated session changed before persistence"
            ) from exc
        _assert_identity_verifier_current()
        current_venue, current_account = _routing_snapshot(client)
        if (
            current.session_context_id != expected_context
            or current.currency_code != expected_currency
            or current_venue != expected_venue
            or current_account != expected_account
            or capture.venue_id != expected_venue
            or capture.account_id != expected_account
        ):
            raise error_type(
                "settlement currency authenticated context changed before persistence"
            )
        _assert_currency_lookup_current()
        legacy_currency = original_currency_for_capture(capture)
        _assert_currency_lookup_current()
        if legacy_currency != expected_currency:
            raise error_type(
                "settlement currency evidence changed before persistence"
            )
        return expected_currency

    return read_currency_qualified_execution_readback, currency_for_capture


(
    read_currency_qualified_execution_readback,
    _currency_for_capture,
) = _install_credential_origin_guard()

if _semantics.read_currency_qualified_execution_readback is not _ORIGINAL_QUALIFIED_READ:
    raise RuntimeError("Betfair settlement currency read dispatch changed before K07 guard")
if _semantics._currency_for_capture is not _ORIGINAL_CURRENCY_FOR_CAPTURE:
    raise RuntimeError("Betfair settlement currency lookup changed before K07 guard")
if _settlement.read_currency_qualified_execution_readback is not _ORIGINAL_QUALIFIED_READ:
    raise RuntimeError("Betfair settlement public currency read changed before K07 guard")

_semantics.read_currency_qualified_execution_readback = (
    read_currency_qualified_execution_readback
)
_semantics._currency_for_capture = _currency_for_capture
_settlement.read_currency_qualified_execution_readback = (
    read_currency_qualified_execution_readback
)

__all__ = ["read_currency_qualified_execution_readback"]
