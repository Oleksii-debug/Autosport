from __future__ import annotations

from dataclasses import replace
import http.client as _http_client
import json
import socket as _socket
from types import FunctionType
import urllib.request as _urllib_request

import pytest

from autosport import betfair_account_identity as account_identity_module
import autosport.betfair_account_readonly as readonly_module
from autosport.betfair_account_identity import (
    BetfairAccountIdentityError,
    build_betfair_authenticated_client,
    resolve_betfair_authenticated_account_identity,
)
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


def _install_https_test_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _Opener()

    def fake_do_open(_self, _http_class, request, **_kwargs):
        response = opener.open(
            request,
            timeout=getattr(request, "timeout", 0),
        )
        response.code = 200
        response.msg = "OK"
        response.info = lambda: {}
        return response

    monkeypatch.setattr(
        _urllib_request.AbstractHTTPHandler,
        "do_open",
        fake_do_open,
    )


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


def test_mocked_https_dispatch_cannot_issue_provider_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_https_test_dispatch(monkeypatch)
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )

    capture = _read(client)

    capture._validate()
    with pytest.raises(BetfairReadOnlyError, match="product-origin authority"):
        capture.assert_authoritative()
    assert capture._authority_client is None
    assert capture._authority_account_identity is None
    assert capture._authority_capture_fingerprint is None
    assert capture._authority_origin_proof is None

def test_mocked_capture_and_equal_copy_both_lack_origin_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_https_test_dispatch(monkeypatch)
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )
    capture = _read(client)
    copied = replace(capture)

    assert copied == capture
    for candidate in (capture, copied):
        with pytest.raises(BetfairReadOnlyError, match="product-origin authority"):
            candidate.assert_authoritative()

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


def test_mocked_capture_stays_non_authoritative_after_transport_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_https_test_dispatch(monkeypatch)
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )
    capture = _read(client)

    with pytest.raises(BetfairReadOnlyError, match="product-origin authority"):
        capture.assert_authoritative()

    client._transport = UrllibBetfairHttpTransport()

    with pytest.raises(BetfairReadOnlyError):
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

    _install_https_test_dispatch(monkeypatch)
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


def _function_with_freevar(function, name: str):
    pending = [function]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if name in current.__code__.co_freevars:
            return current
        for cell in current.__closure__ or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if type(value) is FunctionType:
                pending.append(value)
    raise AssertionError(f"function with freevar not found: {name}")


def _cell(value):
    def capture():
        return value

    assert capture.__closure__ is not None
    return capture.__closure__[0]


def test_mocked_capture_has_no_origin_proof_to_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_https_test_dispatch(monkeypatch)
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )
    capture = _read(client)
    copied = replace(capture)

    assert capture._authority_origin_proof is None
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
    _install_https_test_dispatch(monkeypatch)
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


def test_same_code_reconstruction_with_foreign_raw_read_cannot_mint_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_https_test_dispatch(monkeypatch)
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )

    structural_client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=_CustomTransport(),
        venue_id="betfair",
        account_id="acct-1",
    )
    structural = _read(structural_client)

    lower = _function_with_freevar(
        BetfairReadOnlyClient.read_execution_readback,
        "issue_origin",
    )

    def foreign_raw_read(
        self,
        *,
        action_id: str,
        market_id: str,
        provider_order_ref: str | None = None,
        page_size: int = 1000,
        max_pages: int = 100,
    ):
        del self, action_id, market_id, provider_order_ref, page_size, max_pages
        return structural

    closure = list(lower.__closure__ or ())
    freevars = lower.__code__.co_freevars
    closure[freevars.index("raw_read")] = _cell(foreign_raw_read)
    reconstructed = FunctionType(
        lower.__code__,
        lower.__globals__,
        lower.__name__,
        lower.__defaults__,
        tuple(closure),
    )
    reconstructed.__kwdefaults__ = lower.__kwdefaults__

    capture = reconstructed(
        client,
        action_id=ACTION_ID,
        market_id=MARKET_ID,
        provider_order_ref=PROVIDER_REF,
    )
    assert capture is structural
    with pytest.raises(BetfairReadOnlyError, match="product-origin authority"):
        capture.assert_authoritative()

@pytest.mark.parametrize(
    ("owner", "name"),
    (
        (_urllib_request.HTTPSHandler, "https_open"),
        (_urllib_request.AbstractHTTPHandler, "do_open"),
        (_http_client.HTTPSConnection, "connect"),
        (_http_client.HTTPConnection, "connect"),
        (_socket, "create_connection"),
    ),
)
def test_execution_origin_predicate_rejects_transitive_network_dispatch_rebind(
    monkeypatch: pytest.MonkeyPatch,
    owner,
    name: str,
) -> None:
    predicate = _closure_value(
        BetfairReadOnlyClient.read_execution_readback,
        "origin_dispatch_current",
    )
    assert predicate() is True
    original = getattr(owner, name)

    def hostile(*args, **kwargs):
        del args, kwargs
        raise AssertionError("hostile network dispatch must not become origin authority")

    monkeypatch.setattr(owner, name, hostile)
    assert getattr(owner, name) is not original
    assert predicate() is False


@pytest.mark.parametrize(
    ("name", "replacement"),
    (
        ("Request", object),
        ("_READ_METHOD_ENDPOINT", {}),
        ("BETTING_JSON_RPC_ENDPOINT", "https://example.invalid/json-rpc"),
        ("_LIST_CURRENT_ORDERS", "SportsAPING/v1.0/listClearedOrders"),
    ),
)
def test_execution_origin_predicate_rejects_request_or_endpoint_rebind(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    replacement,
) -> None:
    predicate = _closure_value(
        BetfairReadOnlyClient.read_execution_readback,
        "origin_dispatch_current",
    )
    assert predicate() is True

    monkeypatch.setattr(readonly_module, name, replacement)
    assert predicate() is False


@pytest.mark.parametrize(
    "target",
    (
        UrllibBetfairHttpTransport.post,
        BetfairReadOnlyClient._rpc,
    ),
)
def test_execution_origin_predicate_rejects_in_place_request_code_mutation(
    monkeypatch: pytest.MonkeyPatch,
    target,
) -> None:
    predicate = _closure_value(
        BetfairReadOnlyClient.read_execution_readback,
        "origin_dispatch_current",
    )
    assert predicate() is True
    original_code = target.__code__

    def hostile(*args, **kwargs):
        del args, kwargs
        raise AssertionError("hostile request code must not become provider origin")

    assert len(hostile.__code__.co_freevars) == len(original_code.co_freevars)
    monkeypatch.setattr(target, "__code__", hostile.__code__)
    assert predicate() is False


def test_process_global_urllib_opener_and_mocked_do_open_cannot_mint_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_https_test_dispatch(monkeypatch)

    class HostileGlobalOpener:
        def __init__(self) -> None:
            self.calls = 0

        def open(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError(
                "process-global urllib opener must not serve authenticated readback I/O"
            )

    hostile = HostileGlobalOpener()
    monkeypatch.setattr(_urllib_request, "_opener", hostile)
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="acct-1",
    )

    capture = _read(client)

    assert hostile.calls == 0
    capture._validate()
    with pytest.raises(BetfairReadOnlyError, match="product-origin authority"):
        capture.assert_authoritative()
