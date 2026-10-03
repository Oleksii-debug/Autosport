from __future__ import annotations

from dataclasses import replace
import json
from types import FunctionType
import urllib.request as _urllib_request

import pytest

from autosport.betfair_account_identity import (\n    build_betfair_authenticated_client,\n    resolve_betfair_authenticated_account_identity,\n)
from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BETTING_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
    UrllibBetfairHttpTransport,
)


MARKET_ID = "1.234"
EVENT_ID = "event-1"
ACTION_ID = "action-authenticated-readback"
PROVIDER_REF = "a" * 32


def _result_for_method(method: str) -> object:
    if method == "AccountAPING/v1.0/getAccountDetails":
        return {
            "currencyCode": "EUR",
            "localeCode": "en",
            "region": "GBR",
            "timezone": "Europe/London",
        }
    if method == "SportsAPING/v1.0/listMarketCatalogue":
        return [{"marketId": MARKET_ID, "event": {"id": EVENT_ID}}]
    if method == "SportsAPING/v1.0/listCurrentOrders":
        return {"currentOrders": [], "moreAvailable": False}
    if method == "SportsAPING/v1.0/listClearedOrders":
        return {"clearedOrders": [], "moreAvailable": False}
    raise AssertionError(f"unexpected Betfair method: {method}")


def _response_bytes(body: bytes) -> bytes:
    request = json.loads(body.decode("utf-8"))
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": _result_for_method(request["method"]),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        assert limit >= len(self._payload)
        return self._payload


class _Opener:
    def open(self, request, data=None, timeout: float = 0):
        assert data is None
        assert request.data is not None
        assert request.full_url in (ACCOUNT_JSON_RPC_ENDPOINT, BETTING_JSON_RPC_ENDPOINT)
        assert timeout > 0
        return _Response(_response_bytes(request.data))


class _CustomTransport:
    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        del headers
        assert url == BETTING_JSON_RPC_ENDPOINT
        assert timeout_seconds > 0
        return _response_bytes(body)


def _read(client: BetfairReadOnlyClient):
    return client.read_execution_readback(
        action_id=ACTION_ID,
        market_id=MARKET_ID,
        provider_order_ref=PROVIDER_REF,
    )


def test_k07_product_client_issues_authoritative_execution_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep the canonical Autosport urlopen function/transport intact. Replace only
    # stdlib's process opener below that frozen boundary, matching existing K07 tests.
    monkeypatch.setattr(_urllib_request, "_opener", _Opener())
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )

    capture = _read(client)

    capture.assert_authoritative()


def test_equal_dataclass_copy_loses_readback_origin_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_urllib_request, "_opener", _Opener())
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )
    capture = _read(client)
    copied = replace(capture)

    assert copied == capture
    with pytest.raises(BetfairReadOnlyError, match="product-origin authority"):
        copied.assert_authoritative()


def test_direct_custom_transport_capture_cannot_mint_provider_origin() -> None:
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=_CustomTransport(),
        venue_id="betfair",
        account_id="acct-1",
    )

    capture = _read(client)

    # Structural parsing remains available for read-only fixtures.
    capture._validate()
    with pytest.raises(BetfairReadOnlyError, match="product-origin authority"):
        capture.assert_authoritative()


def test_k07_client_transport_rotation_revokes_existing_readback_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_urllib_request, "_opener", _Opener())
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )
    capture = _read(client)
    capture.assert_authoritative()

    client._transport = UrllibBetfairHttpTransport()

    with pytest.raises(BetfairReadOnlyError, match="no longer authoritative"):
        capture.assert_authoritative()


def test_lower_readback_wrapper_has_no_direct_mutable_issuance_registry() -> None:
    roots = (
        BetfairReadOnlyClient.read_execution_readback,
        type(
            object.__new__(
                __import__(
                    "autosport.betfair_account_readonly",
                    fromlist=["BetfairExecutionReadbackEnvelope"],
                ).BetfairExecutionReadbackEnvelope
            )
        ).assert_authoritative,
    )
    for root in roots:
        for cell in root.__closure__ or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            assert not (
                type(value) is dict
                and any(type(key) is int for key in value)
            ), "lower readback authority still exposes mutable id-key issuance state"


def test_public_authority_slots_cannot_mint_origin_without_canonical_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structural capture cannot become authoritative by filling public dataclass slots."""

    monkeypatch.setattr(_urllib_request, "_opener", _Opener())
    canonical_client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )
    identity = resolve_betfair_authenticated_account_identity(canonical_client)

    structural_client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=_CustomTransport(),
        venue_id="betfair",
        account_id="acct-1",
    )
    forged = _read(structural_client)

    object.__setattr__(forged, "_authority_client", canonical_client)
    object.__setattr__(forged, "_authority_account_identity", identity)
    object.__setattr__(
        forged,
        "_authority_capture_fingerprint",
        forged._authority_fingerprint(),
    )

    with pytest.raises(BetfairReadOnlyError, match="product-origin authority"):
        forged.assert_authoritative()


def _closure_value(function, name: str):
    pending = [function]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        closure = current.__closure__ or ()
        for freevar, cell in zip(current.__code__.co_freevars, closure, strict=True):
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if freevar == name:
                return value
            if type(value) is FunctionType:
                pending.append(value)
    raise AssertionError(f"closure value not found: {name}")


def test_origin_proof_cannot_be_replayed_onto_equal_capture_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_urllib_request, "_opener", _Opener())
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )
    capture = _read(client)
    capture.assert_authoritative()
    copied = replace(capture)

    object.__setattr__(copied, "_authority_client", capture._authority_client)
    object.__setattr__(
        copied,
        "_authority_account_identity",
        capture._authority_account_identity,
    )
    object.__setattr__(
        copied,
        "_authority_capture_fingerprint",
        capture._authority_capture_fingerprint,
    )
    object.__setattr__(
        copied,
        "_authority_origin_proof",
        capture._authority_origin_proof,
    )

    with pytest.raises(BetfairReadOnlyError, match="product-origin authority"):
        copied.assert_authoritative()


def test_closure_recovered_origin_issuer_rejects_external_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_urllib_request, "_opener", _Opener())
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )
    identity = resolve_betfair_authenticated_account_identity(client)
    forged = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=_CustomTransport(),
        venue_id="betfair",
        account_id="acct-1",
    )
    structural = _read(forged)
    issuer = _closure_value(BetfairReadOnlyClient.read_execution_readback, "issue_origin")

    with pytest.raises(
        BetfairAccountIdentityError,
        match="only be issued by canonical acquisition",
    ):
        issuer(
            client,
            identity,
            capture_identity=id(structural),
            capture_fingerprint=structural._authority_fingerprint(),
        )


def test_readback_origin_binder_is_consumed_and_not_publicly_reusable() -> None:
    assert not hasattr(
        account_identity_module,
        "_bind_betfair_execution_readback_origin_authority",
    )
