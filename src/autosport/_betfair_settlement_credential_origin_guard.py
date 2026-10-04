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
    issued: dict[int, tuple[object, object, _IDENTITY_TYPE, str, str]] = {}
    lock = Lock()

    def _require_k07(client, *, stage: str) -> _IDENTITY_TYPE:
        try:
            identity = _RESOLVE_IDENTITY(client)
            return _REQUIRE_IDENTITY(identity, client=client)
        except _IDENTITY_ERROR as exc:
            raise _ERROR(
                f"settlement currency {stage} lacks K07 authenticated client/session authority"
            ) from exc

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
        if type(client) is not _CLIENT_TYPE:
            raise _ERROR(
                "settlement currency acquisition requires exact BetfairReadOnlyClient"
            )

        before = _require_k07(client, stage="acquisition")
        capture = _ORIGINAL_QUALIFIED_READ(
            client,
            action_id=action_id,
            market_id=market_id,
            provider_order_ref=provider_order_ref,
            page_size=page_size,
        )
        if type(capture) is not _CAPTURE_TYPE:
            raise _ERROR("settlement readback is not canonical Betfair execution capture")

        try:
            _REQUIRE_IDENTITY(before, client=client)
        except _IDENTITY_ERROR as exc:
            raise _ERROR(
                "settlement currency authenticated session changed during execution readback"
            ) from exc

        after = _require_k07(client, stage="post-readback verification")
        if (
            before.session_context_id != after.session_context_id
            or before.venue_id != after.venue_id
            or before.currency_code != after.currency_code
        ):
            raise _ERROR(
                "settlement currency/readback authenticated session context changed"
            )

        legacy_currency = _ORIGINAL_CURRENCY_FOR_CAPTURE(capture)
        if legacy_currency is None or legacy_currency != before.currency_code:
            raise _ERROR(
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
            )
        return capture

    def currency_for_capture(capture) -> str | None:
        if type(capture) is not _CAPTURE_TYPE:
            return None
        with lock:
            record = issued.get(id(capture))
        if record is None or record[0]() is not capture:
            return None

        client = record[1]()
        if client is None:
            raise _ERROR(
                "settlement currency authenticated client expired before persistence"
            )
        identity = record[2]
        expected_context = record[3]
        expected_currency = record[4]
        try:
            current = _REQUIRE_IDENTITY(identity, client=client)
        except _IDENTITY_ERROR as exc:
            raise _ERROR(
                "settlement currency authenticated session changed before persistence"
            ) from exc
        if (
            current.session_context_id != expected_context
            or current.currency_code != expected_currency
        ):
            raise _ERROR(
                "settlement currency authenticated context changed before persistence"
            )
        legacy_currency = _ORIGINAL_CURRENCY_FOR_CAPTURE(capture)
        if legacy_currency != expected_currency:
            raise _ERROR(
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
