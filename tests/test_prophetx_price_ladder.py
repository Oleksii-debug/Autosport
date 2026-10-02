from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import autosport.prophetx_price_ladder as ladder_module
from autosport.prophetx_price_ladder import (
    ADAPTER_ID,
    ProphetXPriceLadderClient,
    ProphetXPriceLadderError,
    ProphetXPriceLadderHttpResponse,
    ProphetXPriceLadderTransportError,
    assert_provider_origin_verified,
)


TOKEN = "sandbox-token-secret"
NOW = datetime(2026, 9, 22, 21, 6, 0, tzinfo=timezone.utc)
URL = "https://api.sandbox.prophetx.dev/partner/v4/mm/get_price_ladder"


def _response(body: bytes, **headers: str) -> ProphetXPriceLadderHttpResponse:
    merged = {"Content-Type": "application/json", **headers}
    return ProphetXPriceLadderHttpResponse(200, URL, merged, body)


def _transport_for(body: bytes, *, seen=None, response_headers=None):
    response_headers = response_headers or {}

    def transport(url, headers, timeout_seconds, max_response_bytes):
        if seen is not None:
            seen.append((url, dict(headers), timeout_seconds, max_response_bytes))
        return _response(body, **response_headers)

    return transport


def _client(body=b'{"data":[-110,100,125]}', *, seen=None, response_headers=None):
    return ProphetXPriceLadderClient(
        TOKEN,
        transport=_transport_for(body, seen=seen, response_headers=response_headers),
        clock=lambda: NOW,
    )


def test_read_ladder_binds_exact_payload_and_semantic_membership():
    raw = b'{"data":[-110,100,125]}'
    snapshot = _client(raw).read_ladder()

    assert snapshot.adapter_id == ADAPTER_ID
    assert snapshot.environment == "SANDBOX"
    assert snapshot.endpoint == URL
    assert snapshot.prices == (-110, 100, 125)
    assert snapshot.observed_at == "2026-09-22T21:06:00Z"
    assert snapshot.source_payload_sha256 == hashlib.sha256(raw).hexdigest()
    assert snapshot.contains(-110)
    assert snapshot.contains("125")
    assert not snapshot.contains(130)
    assert snapshot.provider_origin_verified is False
    assert snapshot.current_execution_price_authority is False


def test_transport_is_fixed_origin_get_style_and_bearer_header_only():
    seen = []
    client = _client(seen=seen)
    client.read_ladder()

    assert len(seen) == 1
    url, headers, timeout_seconds, max_response_bytes = seen[0]
    assert url == URL
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in url
    assert headers["Accept"] == "application/json"
    assert headers["Accept-Encoding"] == "identity"
    assert timeout_seconds == 10.0
    assert max_response_bytes == 256 * 1024
    assert TOKEN not in repr(client)


def test_custom_transport_never_claims_verified_provider_origin():
    snapshot = _client().read_ladder()
    assert snapshot.provider_origin_verified is False
    assert snapshot.current_execution_price_authority is False


def test_only_default_fixed_origin_acquisition_can_pass_origin_assertion(monkeypatch):
    raw = b'{"data":[-110,100,125]}'

    def fixed_transport(url, headers, timeout_seconds, max_response_bytes):
        assert url == URL
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        return _response(raw)

    monkeypatch.setattr(ladder_module, "_default_transport", fixed_transport)
    snapshot = ProphetXPriceLadderClient(TOKEN, clock=lambda: NOW).read_ladder()

    assert snapshot.provider_origin_verified is True
    assert_provider_origin_verified(snapshot)


def test_custom_or_caller_constructed_snapshot_cannot_launder_provider_origin():
    custom = _client().read_ladder()
    with pytest.raises(ProphetXPriceLadderError, match="not issued"):
        assert_provider_origin_verified(custom)

    forged = ladder_module.ProphetXPriceLadderSnapshot(
        adapter_id=custom.adapter_id,
        environment=custom.environment,
        endpoint=custom.endpoint,
        prices=custom.prices,
        observed_at=custom.observed_at,
        source_payload_sha256=custom.source_payload_sha256,
        ladder_sha256=custom.ladder_sha256,
        provider_origin_verified=True,
    )
    with pytest.raises(ProphetXPriceLadderError, match="not issued"):
        assert_provider_origin_verified(forged)


def test_semantic_ladder_digest_is_order_independent_but_payload_digest_is_not():
    a = _client(b'{"data":[-110,100,125]}').read_ladder()
    b = _client(b'{"data":[125,-110,100]}').read_ladder()

    assert a.ladder_sha256 == b.ladder_sha256
    assert a.source_payload_sha256 != b.source_payload_sha256


@pytest.mark.parametrize(
    "raw",
    [
        b'[]',
        b'{}',
        b'{"data":[]}',
        b'{"data":{}}',
        b'{"data":[-110,-110]}',
        b'{"data":[0]}',
        b'{"data":[99]}',
        b'{"data":[-99]}',
        b'{"data":[true]}',
        b'{"data":[-109.5]}',
        b'{"data":[NaN]}',
        b'{"data":[Infinity]}',
        b'{"data":[null]}',
    ],
)
def test_malformed_or_noncanonical_ladder_fails_closed(raw):
    with pytest.raises(ProphetXPriceLadderError):
        _client(raw).read_ladder()


def test_integral_json_decimal_is_accepted_exactly():
    snapshot = _client(b'{"data":[-110.0,125.000]}').read_ladder()
    assert snapshot.prices == (-110, 125)


def test_duplicate_json_keys_fail_closed():
    with pytest.raises(ProphetXPriceLadderError, match="duplicate key"):
        _client(b'{"data":[-110],"data":[125]}').read_ladder()


def test_non_utf8_fails_closed():
    with pytest.raises(ProphetXPriceLadderError, match="UTF-8"):
        _client(b'{"data":[-110]}\xff').read_ladder()


def test_invalid_json_fails_closed():
    with pytest.raises(ProphetXPriceLadderError, match="invalid JSON"):
        _client(b'{"data":').read_ladder()


def test_non_json_content_type_fails_before_payload_use():
    response = ProphetXPriceLadderHttpResponse(
        200,
        URL,
        {"Content-Type": "text/html"},
        b'{"data":[-110]}',
    )
    client = ProphetXPriceLadderClient(
        TOKEN,
        transport=lambda *_: response,
        clock=lambda: NOW,
    )
    with pytest.raises(ProphetXPriceLadderTransportError, match="not JSON"):
        client.read_ladder()


def test_encoded_response_fails_closed():
    response = ProphetXPriceLadderHttpResponse(
        200,
        URL,
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
        b'{"data":[-110]}',
    )
    client = ProphetXPriceLadderClient(
        TOKEN,
        transport=lambda *_: response,
        clock=lambda: NOW,
    )
    with pytest.raises(ProphetXPriceLadderTransportError, match="encoding"):
        client.read_ladder()


def test_declared_or_actual_oversize_fails_closed():
    tiny = ProphetXPriceLadderClient(
        TOKEN,
        max_response_bytes=10,
        transport=lambda *_: ProphetXPriceLadderHttpResponse(
            200,
            URL,
            {"Content-Type": "application/json", "Content-Length": "11"},
            b'{}',
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(ProphetXPriceLadderTransportError, match="size limit"):
        tiny.read_ladder()

    actual = ProphetXPriceLadderClient(
        TOKEN,
        max_response_bytes=10,
        transport=lambda *_: ProphetXPriceLadderHttpResponse(
            200,
            URL,
            {"Content-Type": "application/json"},
            b'{"data":[-110]}',
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(ProphetXPriceLadderTransportError, match="size limit"):
        actual.read_ladder()


@pytest.mark.parametrize("status", [0, 201, 400, 401, 500])
def test_non_200_status_never_yields_ladder(status):
    client = ProphetXPriceLadderClient(
        TOKEN,
        transport=lambda *_: ProphetXPriceLadderHttpResponse(
            status,
            URL,
            {"Content-Type": "application/json"},
            b'{"data":[-110]}',
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(ProphetXPriceLadderTransportError):
        client.read_ladder()


def test_arbitrary_transport_exception_text_cannot_leak_token():
    def transport(*_):
        raise RuntimeError(f"provider exploded with Authorization Bearer {TOKEN}")

    client = ProphetXPriceLadderClient(TOKEN, transport=transport, clock=lambda: NOW)
    with pytest.raises(ProphetXPriceLadderTransportError) as caught:
        client.read_ladder()
    assert TOKEN not in str(caught.value)
    assert "Authorization" not in str(caught.value)


@pytest.mark.parametrize("token", ["", " token", "token ", "a\nb", "a\rb", "a\x00b"])
def test_access_token_rejects_empty_whitespace_or_header_injection(token):
    with pytest.raises(ValueError):
        ProphetXPriceLadderClient(token, transport=lambda *_: None)


@pytest.mark.parametrize("value", [True, 1.5, "109.5", "99", "NaN", "Infinity"])
def test_snapshot_membership_rejects_inexact_or_out_of_domain_price(value):
    snapshot = _client().read_ladder()
    with pytest.raises(ProphetXPriceLadderError):
        snapshot.contains(value)


def test_naive_or_non_datetime_clock_fails_closed_after_complete_parse():
    for bad in (lambda: datetime(2026, 9, 22), lambda: "2026-09-22T21:06:00Z"):
        client = ProphetXPriceLadderClient(
            TOKEN,
            transport=_transport_for(b'{"data":[-110]}'),
            clock=bad,
        )
        with pytest.raises(ProphetXPriceLadderError, match="clock"):
            client.read_ladder()


def test_response_payload_digest_changes_when_untrusted_extra_data_changes():
    first = _client(b'{"data":[-110],"meta":{"revision":1}}').read_ladder()
    second = _client(b'{"data":[-110],"meta":{"revision":2}}').read_ladder()
    assert first.prices == second.prices
    assert first.ladder_sha256 == second.ladder_sha256
    assert first.source_payload_sha256 != second.source_payload_sha256


def test_canonical_digest_does_not_depend_on_decimal_context():
    raw = json.dumps({"data": [-110, 100, 125]}, separators=(",", ":")).encode()
    snapshot = _client(raw).read_ladder()
    assert len(snapshot.ladder_sha256) == 64


def test_response_origin_change_fails_closed_even_with_injected_transport():
    client = ProphetXPriceLadderClient(
        TOKEN,
        transport=lambda *_: ProphetXPriceLadderHttpResponse(
            200,
            "https://attacker.invalid/partner/v4/mm/get_price_ladder",
            {"Content-Type": "application/json"},
            b'{"data":[-110]}',
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(ProphetXPriceLadderTransportError, match="origin changed"):
        client.read_ladder()


def test_missing_content_type_fails_closed():
    client = ProphetXPriceLadderClient(
        TOKEN,
        transport=lambda *_: ProphetXPriceLadderHttpResponse(
            200,
            URL,
            {},
            b'{"data":[-110]}',
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(ProphetXPriceLadderTransportError, match="missing Content-Type"):
        client.read_ladder()


def test_declared_content_length_must_match_complete_body():
    raw = b'{"data":[-110]}'
    client = ProphetXPriceLadderClient(
        TOKEN,
        transport=lambda *_: ProphetXPriceLadderHttpResponse(
            200,
            URL,
            {
                "Content-Type": "application/json",
                "Content-Length": str(len(raw) + 1),
            },
            raw,
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(ProphetXPriceLadderTransportError, match="does not match body"):
        client.read_ladder()


def test_invalid_content_length_fails_closed():
    client = ProphetXPriceLadderClient(
        TOKEN,
        transport=lambda *_: ProphetXPriceLadderHttpResponse(
            200,
            URL,
            {"Content-Type": "application/json", "Content-Length": "+12"},
            b'{"data":[-110]}',
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(ProphetXPriceLadderTransportError, match="Content-Length is invalid"):
        client.read_ladder()
