from __future__ import annotations

from email.message import Message
from http.client import IncompleteRead
import pickle

import pytest

import autosport.smarkets_orders_acquisition as acquisition
from autosport.smarkets_orders_acquisition import (
    SMARKETS_ORDERS_ENDPOINT,
    SmarketsOrdersAcquisitionError,
    SmarketsOrdersPayloadWitness,
    acquire_smarkets_orders_payload,
)


class _FakeResponse:
    def __init__(
        self,
        payload: bytes = b'{"orders":[]}',
        *,
        url: str = SMARKETS_ORDERS_ENDPOINT,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        date: str = "Tue, 22 Sep 2026 14:45:00 GMT",
        content_length: str | int | None = None,
        max_chunk: int | None = None,
        incomplete_read: bool = False,
    ) -> None:
        self.payload = payload
        self.url = url
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Date"] = date
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.max_chunk = max_chunk
        self.incomplete_read = incomplete_read
        self.requested_read_sizes: list[int] = []
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status

    def read(self, size=-1):
        self.requested_read_sizes.append(size)
        if self.incomplete_read:
            raise IncompleteRead(self.payload, max(1, len(self.payload)))
        if self._offset >= len(self.payload):
            return b""
        if size < 0:
            size = len(self.payload) - self._offset
        if self.max_chunk is not None:
            size = min(size, self.max_chunk)
        start = self._offset
        self._offset = min(len(self.payload), self._offset + size)
        return self.payload[start : self._offset]


def _install(monkeypatch, response: _FakeResponse):
    captured = {}

    def fake_open(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return response

    monkeypatch.setattr(acquisition, "_open_orders_request", fake_open)
    return captured


def test_fixed_origin_get_issues_exact_payload_witness(monkeypatch) -> None:
    response = _FakeResponse(b'{"orders":[{"id":"o-1"}]}')
    captured = _install(monkeypatch, response)

    witness = acquire_smarkets_orders_payload("secret-token", timeout_seconds=3)

    request = captured["request"]
    assert request.full_url == SMARKETS_ORDERS_ENDPOINT
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Session-Token secret-token"
    assert request.get_header("Accept") == "application/json"
    assert captured["timeout"] == 3.0
    assert witness.endpoint == SMARKETS_ORDERS_ENDPOINT
    assert witness.http_status == 200
    assert witness.provider_date == "2026-09-22T14:45:00+00:00"
    assert witness.payload_bytes == response.payload
    assert witness.payload_size == len(response.payload)
    assert witness.matches_payload(response.payload)
    assert witness.parsed_json() == {"orders": [{"id": "o-1"}]}
    assert len(response.requested_read_sizes) >= 2


def test_witness_cannot_be_constructed_by_ordinary_caller() -> None:
    with pytest.raises(TypeError, match="issued only"):
        SmarketsOrdersPayloadWitness(
            endpoint=SMARKETS_ORDERS_ENDPOINT,
            http_status=200,
            provider_date="2026-09-22T14:45:00+00:00",
            content_type="application/json",
            payload_sha256="0" * 64,
            payload_size=2,
            payload=b"{}",
        )


def test_witness_is_non_serializable_and_payload_bound(monkeypatch) -> None:
    response = _FakeResponse(b'{"orders":[]}')
    _install(monkeypatch, response)
    witness = acquire_smarkets_orders_payload("secret-token")

    assert not witness.matches_payload(b'{"orders":[1]}')
    with pytest.raises(TypeError, match="non-serializable"):
        pickle.dumps(witness)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/v3/orders/",
        "https://api.smarkets.com/v3/orders/?redirected=1",
        "http://api.smarkets.com/v3/orders/",
    ],
)
def test_any_final_url_drift_fails_closed(monkeypatch, url: str) -> None:
    _install(monkeypatch, _FakeResponse(url=url))
    with pytest.raises(SmarketsOrdersAcquisitionError, match="final URL"):
        acquire_smarkets_orders_payload("secret-token")


@pytest.mark.parametrize("status", [201, 204, 301, 401, 429, 500])
def test_only_exact_http_200_is_authoritative(monkeypatch, status: int) -> None:
    _install(monkeypatch, _FakeResponse(status=status))
    with pytest.raises(SmarketsOrdersAcquisitionError, match="non-success status"):
        acquire_smarkets_orders_payload("secret-token")


@pytest.mark.parametrize("content_type", ["text/html", "text/plain", "application/problem+json"])
def test_non_json_media_type_fails_closed(monkeypatch, content_type: str) -> None:
    _install(monkeypatch, _FakeResponse(content_type=content_type))
    with pytest.raises(SmarketsOrdersAcquisitionError, match="application/json"):
        acquire_smarkets_orders_payload("secret-token")


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not-json",
        b'{"x":NaN}',
        b'{"x":1,"x":2}',
        b"\xff",
    ],
)
def test_malformed_or_ambiguous_payload_never_mints_witness(monkeypatch, payload: bytes) -> None:
    _install(monkeypatch, _FakeResponse(payload))
    with pytest.raises(SmarketsOrdersAcquisitionError):
        acquire_smarkets_orders_payload("secret-token")


def test_oversize_payload_fails_closed(monkeypatch) -> None:
    payload = b" " * (8 * 1024 * 1024 + 1)
    _install(monkeypatch, _FakeResponse(payload))
    with pytest.raises(SmarketsOrdersAcquisitionError, match="byte limit"):
        acquire_smarkets_orders_payload("secret-token")


def test_short_reads_without_content_length_are_drained_to_eof(monkeypatch) -> None:
    payload = b'{"orders":[{"id":"o-short"}]}'
    response = _FakeResponse(payload, max_chunk=3)
    _install(monkeypatch, response)

    witness = acquire_smarkets_orders_payload("secret-token")

    assert witness.payload_bytes == payload
    assert len(response.requested_read_sizes) > 3


def test_declared_content_length_is_read_completely_across_short_reads(monkeypatch) -> None:
    payload = b'{"orders":[{"id":"o-complete"}]}'
    response = _FakeResponse(payload, content_length=len(payload), max_chunk=4)
    _install(monkeypatch, response)

    witness = acquire_smarkets_orders_payload("secret-token")

    assert witness.payload_bytes == payload
    assert witness.payload_size == len(payload)


def test_premature_eof_before_declared_content_length_fails_closed(monkeypatch) -> None:
    payload = b'{"orders":[]}'
    response = _FakeResponse(payload, content_length=len(payload) + 7, max_chunk=4)
    _install(monkeypatch, response)

    with pytest.raises(SmarketsOrdersAcquisitionError, match="before Content-Length"):
        acquire_smarkets_orders_payload("secret-token")


def test_incomplete_read_exception_fails_closed(monkeypatch) -> None:
    response = _FakeResponse(b'{"orders":[]}', incomplete_read=True)
    _install(monkeypatch, response)

    with pytest.raises(SmarketsOrdersAcquisitionError, match="body is incomplete"):
        acquire_smarkets_orders_payload("secret-token")


@pytest.mark.parametrize("content_length", ["-1", "not-a-number", "1.5", "  "])
def test_invalid_content_length_fails_closed(monkeypatch, content_length: str) -> None:
    response = _FakeResponse(content_length=content_length)
    _install(monkeypatch, response)

    with pytest.raises(SmarketsOrdersAcquisitionError, match="Content-Length is invalid"):
        acquire_smarkets_orders_payload("secret-token")


def test_declared_oversize_content_length_fails_before_body_is_trusted(monkeypatch) -> None:
    response = _FakeResponse(content_length=8 * 1024 * 1024 + 1)
    _install(monkeypatch, response)

    with pytest.raises(SmarketsOrdersAcquisitionError, match="byte limit"):
        acquire_smarkets_orders_payload("secret-token")
    assert response.requested_read_sizes == []


def test_extra_bytes_beyond_declared_content_length_fail_closed(monkeypatch) -> None:
    payload = b'{"orders":[]}'
    response = _FakeResponse(payload, content_length=len(payload) - 1)
    _install(monkeypatch, response)

    with pytest.raises(SmarketsOrdersAcquisitionError, match="exceeds declared Content-Length"):
        acquire_smarkets_orders_payload("secret-token")


def test_duplicate_content_length_headers_fail_closed(monkeypatch) -> None:
    response = _FakeResponse()
    response.headers["Content-Length"] = str(len(response.payload))
    response.headers["Content-Length"] = str(len(response.payload))
    _install(monkeypatch, response)

    with pytest.raises(SmarketsOrdersAcquisitionError, match="ambiguous Content-Length"):
        acquire_smarkets_orders_payload("secret-token")


@pytest.mark.parametrize("token", ["", " token", "token ", "a\nb", "a\rb", "x" * 4097, None, 1])
def test_session_token_validation_is_strict(monkeypatch, token) -> None:
    _install(monkeypatch, _FakeResponse())
    with pytest.raises(SmarketsOrdersAcquisitionError):
        acquire_smarkets_orders_payload(token)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [0, 0.01, 61, True, "1", None])
def test_timeout_is_bounded(monkeypatch, timeout) -> None:
    _install(monkeypatch, _FakeResponse())
    with pytest.raises(SmarketsOrdersAcquisitionError, match="timeout_seconds"):
        acquire_smarkets_orders_payload("secret-token", timeout_seconds=timeout)  # type: ignore[arg-type]


def test_token_never_appears_in_validation_error(monkeypatch) -> None:
    token = "TOP-SECRET-SESSION-TOKEN"
    _install(monkeypatch, _FakeResponse(url="https://evil.example/v3/orders/"))
    with pytest.raises(SmarketsOrdersAcquisitionError) as exc_info:
        acquire_smarkets_orders_payload(token)
    assert token not in str(exc_info.value)
    assert token not in repr(exc_info.value)
