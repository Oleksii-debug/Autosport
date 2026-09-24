from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from http.client import HTTPException, HTTPSConnection
import ssl
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, OpenerDirector, ProxyHandler

import pytest

import autosport.prophetx_account_readonly as subject
from autosport.bookmaker_capability import BookmakerCapability
from autosport.prophetx_account_readonly import (
    ADAPTER_ID,
    BALANCE_URL,
    PROVIDER_CURRENCY,
    ProphetXHttpResponse,
    ProphetXReadOnlyClient,
    ProphetXReadOnlyError,
    ProphetXSessionToken,
    UrllibProphetXHttpTransport,
)


FIXED_NOW = datetime(2026, 9, 22, 18, 59, tzinfo=timezone.utc)
GOOD_BODY = (
    b'{"data":{"balance":1000.00,"gec_balance":500.00,'
    b'"matched_order_balance":200.00,"unmatched_order_balance":50.00,'
    b'"unmatched_order_balance_status":"succeed",'
    b'"unmatched_order_last_synced_at":"2026-08-10T14:12:40.307908108Z"}}'
)


class FakeTransport:
    def __init__(self, responses: list[ProphetXHttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
    ) -> ProphetXHttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def http_response(
    body: bytes = GOOD_BODY,
    *,
    status: int = 200,
    final_url: str = BALANCE_URL,
    content_type: str | None = "application/json; charset=utf-8",
    content_encoding: str | None = None,
) -> ProphetXHttpResponse:
    return ProphetXHttpResponse(
        status=status,
        final_url=final_url,
        content_type=content_type,
        content_encoding=content_encoding,
        body=body,
    )


def client_for(*responses: ProphetXHttpResponse):
    transport = FakeTransport(list(responses))
    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
    )
    return client, transport


@pytest.mark.parametrize("mutation", ["open", "_open", "https_open"])
def test_canonical_wallet_authority_rejects_preconstruction_stdlib_rebind(
    monkeypatch,
    mutation: str,
):
    forged_calls: list[str] = []

    if mutation == "open":
        def forged(self, request, timeout=None):
            del self, timeout
            forged_calls.append(request.full_url)
            raise AssertionError("forged opener dispatch must not run")

        monkeypatch.setattr(OpenerDirector, "open", forged)
    elif mutation == "_open":
        def forged(self, request, data=None):
            del self, data
            forged_calls.append(request.full_url)
            raise AssertionError("forged internal opener dispatch must not run")

        monkeypatch.setattr(OpenerDirector, "_open", forged)
    else:
        def forged(self, request):
            del self
            forged_calls.append(request.full_url)
            raise AssertionError("forged HTTPS dispatch must not run")

        monkeypatch.setattr(HTTPSHandler, "https_open", forged)

    with pytest.raises(
        ProphetXReadOnlyError,
        match="network dispatch changed before construction",
    ):
        ProphetXReadOnlyClient(
            ProphetXSessionToken("session-secret"),
            clock=lambda: FIXED_NOW,
        )

    assert forged_calls == []


def test_canonical_wallet_authority_rejects_coordinated_tripwire_rebind(
    monkeypatch,
):
    forged_calls: list[str] = []

    def forged(self, request, timeout=None):
        del self, timeout
        forged_calls.append(request.full_url)
        raise AssertionError("forged opener dispatch must not run")

    monkeypatch.setattr(
        subject,
        "_wallet_stdlib_dispatch_is_canonical",
        lambda: True,
    )
    monkeypatch.setattr(subject, "_canonical_wallet_opener_open", forged)
    monkeypatch.setattr(OpenerDirector, "open", forged)

    with pytest.raises(
        ProphetXReadOnlyError,
        match="network dispatch changed before construction",
    ):
        ProphetXReadOnlyClient(
            ProphetXSessionToken("session-secret"),
            clock=lambda: FIXED_NOW,
        )

    assert forged_calls == []


def test_canonical_transport_ignores_provider_fetch_factory_module_rebind(
    monkeypatch,
):
    forged_calls: list[object] = []

    def forged_factory(*args, **kwargs):
        forged_calls.append((args, kwargs))
        raise AssertionError("forged provider-fetch factory must not run")

    monkeypatch.setattr(subject, "_make_provider_fetch", forged_factory)

    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        clock=lambda: FIXED_NOW,
    )

    assert type(client._transport) is UrllibProphetXHttpTransport
    assert forged_calls == []


def test_wallet_read_is_fixed_origin_get_and_preserves_exact_provider_money():
    client, transport = client_for(http_response())

    wallet = client.read_wallet()

    assert wallet.balance == Decimal("1000.00")
    assert wallet.gec_balance == Decimal("500.00")
    assert wallet.matched_order_balance == Decimal("200.00")
    assert wallet.unmatched_order_balance == Decimal("50.00")
    assert wallet.unmatched_order_balance_status == "succeed"
    assert (
        wallet.unmatched_order_last_synced_at
        == "2026-08-10T14:12:40.307908108Z"
    )
    assert wallet.evidence.observed_at == FIXED_NOW.isoformat()
    assert (
        wallet.evidence.source_payload_sha256
        == sha256(GOOD_BODY).hexdigest()
    )

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == BALANCE_URL
    assert call["headers"] == {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Authorization": "Bearer session-secret",
    }
    assert call["timeout_seconds"] == 10.0



def test_wallet_projection_keeps_credit_and_locked_funds_distinct_from_cash():
    client, _ = client_for(http_response())

    wallet = client.read_wallet()
    profile = client._profile_for(wallet)

    assert wallet.balance == Decimal("1000.00")
    assert wallet.gec_balance == Decimal("500.00")
    assert wallet.matched_order_balance == Decimal("200.00")
    assert wallet.unmatched_order_balance == Decimal("50.00")
    assert profile.facts[0].capability is BookmakerCapability.BALANCE_READ
    assert profile.source_payload_sha256 == sha256(GOOD_BODY).hexdigest()
    assert profile.source_ref.startswith("prophetx://sandbox/wallet/")

def test_injected_transport_can_parse_wallet_but_cannot_mint_positive_authority():
    client, transport = client_for(
        http_response(),
        http_response(),
        http_response(),
    )

    assert client.read_wallet().balance == Decimal("1000.00")

    with pytest.raises(
        ProphetXReadOnlyError,
        match="requires product-owned transport",
    ):
        client.capability_profile()

    with pytest.raises(
        ProphetXReadOnlyError,
        match="requires product-owned transport",
    ):
        client.read_account_snapshot(
            frozenset({BookmakerCapability.BALANCE_READ})
        )

    assert len(transport.calls) == 3


def test_replacing_product_owned_transport_invalidates_positive_authority():
    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        clock=lambda: FIXED_NOW,
    )
    replacement = FakeTransport([http_response()])
    client._transport = replacement  # type: ignore[assignment]

    with pytest.raises(
        ProphetXReadOnlyError,
        match="requires product-owned transport",
    ):
        client.read_account_snapshot(
            frozenset({BookmakerCapability.BALANCE_READ})
        )

    assert replacement.calls == []


def test_canonical_wallet_authority_rejects_class_get_rebinding_before_network(
    monkeypatch,
):
    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        clock=lambda: FIXED_NOW,
    )
    calls: list[str] = []

    def fake_get(self, url, *, headers, timeout_seconds):
        calls.append(url)
        return http_response()

    monkeypatch.setattr(UrllibProphetXHttpTransport, "get", fake_get)

    with pytest.raises(
        ProphetXReadOnlyError,
        match="requires product-owned transport",
    ):
        client.read_account_snapshot(
            frozenset({BookmakerCapability.BALANCE_READ})
        )

    assert calls == []


def test_canonical_wallet_authority_rejects_hidden_fetch_replacement_before_network():
    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        clock=lambda: FIXED_NOW,
    )
    calls: list[str] = []

    def fake_fetch(url, *, headers, timeout_seconds):
        calls.append(url)
        return http_response()

    client._transport._provider_fetch = fake_fetch  # type: ignore[attr-defined]

    with pytest.raises(
        ProphetXReadOnlyError,
        match="requires product-owned transport",
    ):
        client.read_account_snapshot(
            frozenset({BookmakerCapability.BALANCE_READ})
        )

    assert calls == []


def _forged_wallet_network_response():
    class ForgedResponse:
        code = 200
        msg = "OK"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(GOOD_BODY)),
        }

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, limit: int) -> bytes:
            return GOOD_BODY

        def getcode(self) -> int:
            return self.code

        def geturl(self) -> str:
            return BALANCE_URL

        def info(self):
            return self.headers

    return ForgedResponse()


@pytest.mark.parametrize("mutation", ["instance-shadow", "dispatch-map"])
def test_canonical_wallet_authority_rejects_hidden_opener_handler_graph_mutation(
    mutation: str,
):
    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        clock=lambda: FIXED_NOW,
    )
    opener = client._transport._opener  # type: ignore[attr-defined]
    https_handler = next(
        handler
        for handler in opener.handlers
        if type(handler) is HTTPSHandler
    )
    calls: list[str] = []

    def forged_https_open(request):
        calls.append(request.full_url)
        return _forged_wallet_network_response()

    if mutation == "instance-shadow":
        https_handler.https_open = forged_https_open
    else:
        class ForgedHttpsHandler:
            def https_open(self, request):
                return forged_https_open(request)

        opener.handle_open["https"] = [ForgedHttpsHandler()]

    with pytest.raises(
        ProphetXReadOnlyError,
        match="network authority changed",
    ):
        client.read_account_snapshot(
            frozenset({BookmakerCapability.BALANCE_READ})
        )

    assert calls == []


@pytest.mark.parametrize("operation", ["profile", "snapshot"])
def test_canonical_account_issuance_ignores_instance_shadowed_read_wallet(
    operation: str,
):
    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        clock=lambda: FIXED_NOW,
    )
    forged_wallet = subject.ProphetXWalletObservation(
        balance=Decimal("999999.00"),
        gec_balance=Decimal("0"),
        matched_order_balance=Decimal("0"),
        unmatched_order_balance=Decimal("0"),
        unmatched_order_balance_status="succeed",
        unmatched_order_last_synced_at="2026-08-10T00:00:00Z",
        evidence=subject.ProphetXEvidence(
            FIXED_NOW.isoformat(),
            "f" * 64,
        ),
    )
    shadow_calls: list[str] = []

    def forged_read_wallet():
        shadow_calls.append("shadow")
        return forged_wallet

    setattr(client, "read_wallet", forged_read_wallet)

    opener = client._transport._opener  # type: ignore[attr-defined]
    https_handler = next(
        handler
        for handler in opener.handlers
        if type(handler) is HTTPSHandler
    )
    network_calls: list[str] = []

    def forged_https_open(request):
        network_calls.append(request.full_url)
        return _forged_wallet_network_response()

    https_handler.https_open = forged_https_open

    with pytest.raises(
        ProphetXReadOnlyError,
        match="network authority changed",
    ):
        if operation == "profile":
            client.capability_profile()
        else:
            client.read_account_snapshot(
                frozenset({BookmakerCapability.BALANCE_READ})
            )

    assert shadow_calls == []
    assert network_calls == []


@pytest.mark.parametrize("operation", ["profile", "snapshot"])
def test_canonical_account_issuance_ignores_module_network_resolver_rebind(
    monkeypatch,
    operation: str,
):
    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        clock=lambda: FIXED_NOW,
    )
    resolver_calls: list[object] = []
    fetch_calls: list[str] = []

    def forged_fetch(url, *, headers, timeout_seconds):
        del headers, timeout_seconds
        fetch_calls.append(url)
        return http_response()

    def forged_resolver(candidate):
        resolver_calls.append(candidate)
        return forged_fetch

    monkeypatch.setattr(
        subject,
        "_require_canonical_network_authority",
        forged_resolver,
    )

    opener = client._transport._opener  # type: ignore[attr-defined]
    https_handler = next(
        handler
        for handler in opener.handlers
        if type(handler) is HTTPSHandler
    )
    network_calls: list[str] = []

    def forged_https_open(request):
        network_calls.append(request.full_url)
        return _forged_wallet_network_response()

    https_handler.https_open = forged_https_open

    with pytest.raises(
        ProphetXReadOnlyError,
        match="network authority changed",
    ):
        if operation == "profile":
            client.capability_profile()
        else:
            client.read_account_snapshot(
                frozenset({BookmakerCapability.BALANCE_READ})
            )

    assert resolver_calls == []
    assert fetch_calls == []
    assert network_calls == []


@pytest.mark.parametrize("mutation", ["replace", "weaken-in-place"])

def test_canonical_wallet_authority_rejects_tls_verifier_state_weakening(
    monkeypatch,
    mutation: str,
):
    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        clock=lambda: FIXED_NOW,
    )
    opener = client._transport._opener  # type: ignore[attr-defined]
    https_handler = next(
        handler
        for handler in opener.handlers
        if type(handler) is HTTPSHandler
    )
    network_calls: list[str] = []

    def forbidden_connect(connection):
        network_calls.append(type(connection).__name__)
        raise AssertionError("network must not start after TLS authority drift")

    monkeypatch.setattr(HTTPSConnection, "connect", forbidden_connect)

    if mutation == "replace":
        insecure_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        insecure_context.check_hostname = False
        insecure_context.verify_mode = ssl.CERT_NONE
        https_handler._context = insecure_context
    else:
        context = https_handler._context
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    with pytest.raises(
        ProphetXReadOnlyError,
        match="network authority changed",
    ):
        client.read_account_snapshot(
            frozenset({BookmakerCapability.BALANCE_READ})
        )

    assert network_calls == []

def test_default_transport_owns_explicit_secure_tls_context():
    transport = UrllibProphetXHttpTransport()
    opener = transport._opener  # type: ignore[attr-defined]
    https_handler = next(
        handler
        for handler in opener.handlers
        if type(handler) is HTTPSHandler
    )
    context = https_handler._context

    assert type(context) is ssl.SSLContext
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    if hasattr(https_handler, "_check_hostname"):
        assert https_handler._check_hostname is None



def test_canonical_wallet_authority_rejects_legacy_hostname_override_before_network(
    monkeypatch,
):
    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        clock=lambda: FIXED_NOW,
    )
    opener = client._transport._opener  # type: ignore[attr-defined]
    https_handler = next(
        handler
        for handler in opener.handlers
        if type(handler) is HTTPSHandler
    )
    if not hasattr(https_handler, "_check_hostname"):
        pytest.skip("legacy HTTPSHandler hostname override is absent")

    network_calls: list[str] = []

    def forbidden_connect(connection):
        network_calls.append(type(connection).__name__)
        raise AssertionError("network must not start after hostname authority drift")

    monkeypatch.setattr(HTTPSConnection, "connect", forbidden_connect)
    https_handler._check_hostname = False

    with pytest.raises(
        ProphetXReadOnlyError,
        match="network authority changed",
    ):
        client.read_account_snapshot(
            frozenset({BookmakerCapability.BALANCE_READ})
        )

    assert network_calls == []

def test_failed_unmatched_balance_sync_is_preserved_but_cannot_mint_account_snapshot():
    failed = GOOD_BODY.replace(b'"succeed"', b'"failed"')
    client, _ = client_for(
        http_response(failed),
        http_response(failed),
    )

    wallet = client.read_wallet()
    assert wallet.unmatched_order_balance_status == "failed"

    with pytest.raises(
        ProphetXReadOnlyError,
        match="synchronization is not successful",
    ):
        client.read_account_snapshot(
            frozenset({BookmakerCapability.BALANCE_READ})
        )


def test_unknown_unmatched_sync_status_fails_closed():
    body = GOOD_BODY.replace(b'"succeed"', b'"unknown"')
    client, _ = client_for(http_response(body))

    with pytest.raises(
        ProphetXReadOnlyError,
        match="unmatched_order_balance_status",
    ):
        client.read_wallet()


def test_successful_unmatched_sync_requires_a_timestamp():
    body = GOOD_BODY.replace(
        b'"2026-08-10T14:12:40.307908108Z"',
        b"null",
    )
    client, _ = client_for(http_response(body))

    with pytest.raises(
        ProphetXReadOnlyError,
        match="requires last-synced timestamp",
    ):
        client.read_wallet()


@pytest.mark.parametrize(
    "body,match",
    [
        (
            b'{"data":{"balance":1,"balance":2,"gec_balance":0,'
            b'"matched_order_balance":0,"unmatched_order_balance":0,'
            b'"unmatched_order_balance_status":"succeed",'
            b'"unmatched_order_last_synced_at":"2026-08-10T00:00:00Z"}}',
            "duplicate object key",
        ),
        (
            GOOD_BODY.replace(b"1000.00", b"NaN"),
            "non-standard numeric constant",
        ),
        (
            GOOD_BODY.replace(b"500.00", b"Infinity"),
            "non-standard numeric constant",
        ),
        (
            GOOD_BODY.replace(b"200.00", b"-0.01"),
            "must be non-negative",
        ),
        (b"not-json", "valid UTF-8 JSON"),
        (b"[]", "must be a JSON object"),
        (b'{"data":[]}', "must be a JSON object"),
    ],
)
def test_malformed_or_unsafe_wallet_payload_fails_closed(
    body: bytes,
    match: str,
):
    client, _ = client_for(http_response(body))

    with pytest.raises(ProphetXReadOnlyError, match=match):
        client.read_wallet()


@pytest.mark.parametrize(
    "response,match",
    [
        (http_response(status=401), "status 401"),
        (http_response(status=403), "status 403"),
        (http_response(status=429), "status 429"),
        (
            http_response(
                final_url="https://example.invalid/redirected"
            ),
            "origin changed unexpectedly",
        ),
        (
            http_response(content_type="text/html"),
            "Content-Type",
        ),
        (
            http_response(content_type=None),
            "missing Content-Type",
        ),
        (
            http_response(content_encoding="gzip"),
            "unsupported content encoding",
        ),
    ],
)
def test_http_origin_and_representation_contract_fails_closed(
    response,
    match: str,
):
    client, _ = client_for(response)

    with pytest.raises(ProphetXReadOnlyError, match=match):
        client.read_wallet()


def test_oversized_response_is_rejected_even_with_injected_transport():
    client, _ = client_for(
        http_response(b"x" * (8 * 1024 * 1024 + 1))
    )

    with pytest.raises(
        ProphetXReadOnlyError,
        match="exceeded the size limit",
    ):
        client.read_wallet()


def test_unsupported_capability_is_rejected_before_network_call():
    client, transport = client_for()

    with pytest.raises(
        ProphetXReadOnlyError,
        match="implements only balance_read",
    ):
        client.read_account_snapshot(
            frozenset(
                {BookmakerCapability.OPEN_POSITIONS_READ}
            )
        )

    assert transport.calls == []


def test_empty_capability_request_is_rejected_before_network_call():
    client, transport = client_for()

    with pytest.raises(
        ProphetXReadOnlyError,
        match="at least one account capability",
    ):
        client.read_account_snapshot(frozenset())

    assert transport.calls == []


def test_wrong_capability_collection_type_is_rejected_before_network_call():
    client, transport = client_for()

    with pytest.raises(TypeError, match="frozenset"):
        client.read_account_snapshot(
            {BookmakerCapability.BALANCE_READ}  # type: ignore[arg-type]
        )

    assert transport.calls == []



def test_secrets_are_redacted_from_reprs_and_structural_wallet_evidence():
    session = ProphetXSessionToken("session-secret")
    client, _ = client_for(http_response())

    wallet = client.read_wallet()
    profile = client._profile_for(wallet)

    assert "session-secret" not in repr(session)
    assert "session-secret" not in repr(client)
    assert "session-secret" not in repr(wallet)
    assert "session-secret" not in repr(profile)
    assert "session-secret" not in profile.source_ref

def test_product_observation_clock_is_not_read_until_payload_is_accepted():
    calls: list[str] = []

    def clock() -> datetime:
        calls.append("clock")
        return FIXED_NOW

    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        transport=FakeTransport([http_response(b"not-json")]),
        clock=clock,
    )

    with pytest.raises(
        ProphetXReadOnlyError,
        match="valid UTF-8 JSON",
    ):
        client.read_wallet()

    assert calls == []


@pytest.mark.parametrize(
    "timeout",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        0.0,
        -1.0,
    ],
)
def test_timeout_must_be_positive_and_finite(timeout: float):
    with pytest.raises(
        ValueError,
        match="positive and finite",
    ):
        ProphetXReadOnlyClient(
            ProphetXSessionToken("session-secret"),
            transport=FakeTransport([]),
            timeout_seconds=timeout,
        )


def test_clock_must_be_timezone_aware():
    client = ProphetXReadOnlyClient(
        ProphetXSessionToken("session-secret"),
        transport=FakeTransport([http_response()]),
        clock=lambda: datetime(2026, 9, 22, 18, 59),
    )

    with pytest.raises(
        ProphetXReadOnlyError,
        match="timezone-aware",
    ):
        client.read_wallet()


@pytest.mark.parametrize(
    "token",
    [
        "abc def",
        "abc\tdef",
        "abc\r\nX-Injected: yes",
        "abc\x7fdef",
    ],
)
def test_session_token_rejects_whitespace_and_header_control_characters(token: str):
    with pytest.raises(
        ProphetXReadOnlyError,
        match="whitespace or control characters",
    ):
        ProphetXSessionToken(token)


def test_default_transport_disables_environment_proxies():
    transport = UrllibProphetXHttpTransport()

    proxy_handlers = [
        handler
        for handler in transport._opener.handlers  # type: ignore[attr-defined]
        if isinstance(handler, ProxyHandler)
    ]

    # urllib.build_opener intentionally omits an empty ProxyHandler from
    # opener.handlers because it has no proxy_* dispatch methods to register.
    # The security contract is therefore absence of any configured proxy handler,
    # not presence of an inert empty handler object.
    assert proxy_handlers == []


@pytest.mark.parametrize(
    "content_length,match",
    [
        ("999", "does not match body"),
        ("not-a-number", "Content-Length is invalid"),
    ],
)
def test_default_transport_rejects_invalid_content_length(
    content_length: str,
    match: str,
):
    class FakeResponse:
        def __init__(self) -> None:
            self.headers = {
                "Content-Type": "application/json",
                "Content-Length": content_length,
            }

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, limit: int) -> bytes:
            return GOOD_BODY

        def getcode(self) -> int:
            return 200

        def geturl(self) -> str:
            return BALANCE_URL

    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse()

    transport = UrllibProphetXHttpTransport()
    transport._opener = FakeOpener()  # type: ignore[assignment]

    with pytest.raises(ProphetXReadOnlyError, match=match):
        transport.get(
            BALANCE_URL,
            headers={},
            timeout_seconds=1.0,
        )


def test_default_transport_normalizes_http_protocol_failure():
    class FailingOpener:
        def open(self, request, timeout):
            raise HTTPException("truncated")

    transport = UrllibProphetXHttpTransport()
    transport._opener = FailingOpener()  # type: ignore[assignment]

    with pytest.raises(
        ProphetXReadOnlyError,
        match="network request failed",
    ):
        transport.get(
            BALANCE_URL,
            headers={},
            timeout_seconds=1.0,
        )


def test_default_transport_closes_http_error_response_before_failing_closed():
    class CloseTrackingBody:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    body = CloseTrackingBody()
    error = HTTPError(
        BALANCE_URL,
        401,
        "Unauthorized",
        {},
        body,
    )

    class FailingOpener:
        def open(self, request, timeout):
            raise error

    transport = UrllibProphetXHttpTransport()
    transport._opener = FailingOpener()  # type: ignore[assignment]

    with pytest.raises(
        ProphetXReadOnlyError,
        match="status 401",
    ):
        transport.get(
            BALANCE_URL,
            headers={},
            timeout_seconds=1.0,
        )

    assert body.closed is True


def test_blank_session_token_is_rejected():
    with pytest.raises(
        ProphetXReadOnlyError,
        match="access_token",
    ):
        ProphetXSessionToken(" ")
