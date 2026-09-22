from __future__ import annotations

from email.message import Message
from hashlib import sha256
from io import BytesIO
import pickle
from urllib.error import HTTPError

import pytest

import autosport.smarkets_orders_acquisition as orders_acquisition
import autosport.smarkets_session_context as session_context
from autosport.smarkets_session_context import (
    SMARKETS_ACCOUNTS_ENDPOINT,
    SmarketsAuthenticatedSession,
    SmarketsSessionContextError,
    SmarketsSessionOrdersReadback,
    open_smarkets_authenticated_session,
)


def _account_payload(
    account_id: str = "provider-account-A",
    *,
    extra: str = "",
) -> bytes:
    extra_fragment = f',"opaque":"{extra}"' if extra else ""
    return (
        '{"account":{'
        f'"account_id":"{account_id}",'
        '"balance":"1000.00",'
        '"available_balance":"900.00",'
        '"exposure":"100.00",'
        '"currency":"GBP"'
        f'{extra_fragment}'
        '}}'
    ).encode("utf-8")


class _FakeResponse:
    def __init__(
        self,
        payload: bytes | None = None,
        *,
        url: str = SMARKETS_ACCOUNTS_ENDPOINT,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        date: str = "Mon, 21 Sep 2026 10:00:00 GMT",
        declare_length: bool = True,
    ) -> None:
        if payload is None:
            payload = _account_payload()
        self._stream = BytesIO(payload)
        self.payload = payload
        self.url = url
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Date"] = date
        if declare_length:
            self.headers["Content-Length"] = str(len(payload))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self._stream.read(size)


def _install_accounts(monkeypatch, response: _FakeResponse):
    captured = {}

    def fake_open(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return response

    monkeypatch.setattr(session_context, "_open_accounts_request", fake_open)
    return captured


def _install_identity(
    monkeypatch,
    *,
    generation_ids=("generation-A",),
    times=("2026-09-22T18:00:00+00:00",),
):
    generation_iter = iter(generation_ids)
    time_iter = iter(times)
    monkeypatch.setattr(session_context, "_new_generation_id", lambda: next(generation_iter))
    monkeypatch.setattr(session_context, "_utc_now_iso", lambda: next(time_iter))


def _orders_record(payload: bytes = b'{"orders":[]}'):
    return orders_acquisition.SmarketsOrdersAcquisitionRecord(
        endpoint=orders_acquisition.SMARKETS_ORDERS_ENDPOINT,
        http_status=200,
        provider_date="2026-09-22T17:59:59+00:00",
        received_at="2026-09-22T18:00:00+00:00",
        content_type="application/json",
        payload_sha256=sha256(payload).hexdigest(),
        payload_size=len(payload),
        payload=payload,
    )


def test_open_session_uses_fixed_accounts_origin_and_keeps_secret_out_of_evidence(
    monkeypatch,
) -> None:
    secret = "session-secret-sentinel"
    payload = _account_payload("provider-account-A", extra="provider-owned-value")
    response = _FakeResponse(payload)
    captured = _install_accounts(monkeypatch, response)
    _install_identity(monkeypatch)

    session = open_smarkets_authenticated_session(secret, timeout_seconds=4)

    request = captured["request"]
    assert request.full_url == SMARKETS_ACCOUNTS_ENDPOINT
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Session-Token {secret}"
    assert request.get_header("Accept") == "application/json"
    assert captured["timeout"] == 4.0

    assert isinstance(session, SmarketsAuthenticatedSession)
    assert session.generation_id == "generation-A"
    assert session.account_witness.endpoint == SMARKETS_ACCOUNTS_ENDPOINT
    assert session.account_witness.http_status == 200
    assert session.account_witness.provider_date == "2026-09-21T10:00:00+00:00"
    assert session.account_witness.product_available_at == "2026-09-22T18:00:00+00:00"
    assert session.account_witness.payload_sha256 == sha256(payload).hexdigest()
    assert session.account_witness.payload_size == len(payload)
    assert session.account_witness.provider_account_id == "provider-account-A"
    assert session.provider_account_id == "provider-account-A"
    assert secret not in repr(session)
    assert secret not in repr(session.account_witness)
    assert b"provider-owned-value" not in repr(session.account_witness).encode()

    with pytest.raises(TypeError):
        pickle.dumps(session)
    with pytest.raises(TypeError):
        pickle.dumps(session.account_witness)


def test_orders_readback_reuses_exact_runtime_token_and_binds_same_generation(
    monkeypatch,
) -> None:
    secret = "same-runtime-secret"
    _install_accounts(monkeypatch, _FakeResponse())
    _install_identity(
        monkeypatch,
        times=(
            "2026-09-22T18:00:00+00:00",
            "2026-09-22T18:00:02+00:00",
        ),
    )
    session = open_smarkets_authenticated_session(secret)

    captured = {}

    def fake_orders(token, *, timeout_seconds):
        captured["token"] = token
        captured["timeout"] = timeout_seconds
        return _orders_record()

    monkeypatch.setattr(
        orders_acquisition,
        "acquire_smarkets_orders_payload",
        fake_orders,
    )

    readback = session.acquire_orders(timeout_seconds=3)

    assert captured == {"token": secret, "timeout": 3.0}
    assert readback.session_generation_id == session.generation_id
    assert readback.account_context_sha256 == session.account_context_sha256
    assert readback.provider_account_id == session.provider_account_id
    assert readback.accounts_payload_sha256 == session.account_witness.payload_sha256
    assert readback.accounts_available_at == session.account_witness.product_available_at
    assert readback.orders_payload_sha256 == readback.orders_record.payload_sha256
    assert readback.orders_received_at == "2026-09-22T18:00:00+00:00"
    assert readback.session_bound_at == "2026-09-22T18:00:02+00:00"
    assert session.resolve_orders_readback(readback) is readback


def test_two_sessions_with_identical_provider_bytes_do_not_alias(monkeypatch) -> None:
    response_a = _FakeResponse()
    response_b = _FakeResponse()
    responses = iter((response_a, response_b))
    monkeypatch.setattr(
        session_context,
        "_open_accounts_request",
        lambda request, timeout: next(responses),
    )
    _install_identity(
        monkeypatch,
        generation_ids=("generation-A", "generation-B"),
        times=(
            "2026-09-22T18:00:00+00:00",
            "2026-09-22T18:00:00+00:00",
            "2026-09-22T18:00:01+00:00",
            "2026-09-22T18:00:01+00:00",
        ),
    )
    session_a = open_smarkets_authenticated_session("token-A")
    session_b = open_smarkets_authenticated_session("token-B")

    order_record = _orders_record()
    monkeypatch.setattr(
        orders_acquisition,
        "acquire_smarkets_orders_payload",
        lambda token, *, timeout_seconds: order_record,
    )
    readback_a = session_a.acquire_orders()
    readback_b = session_b.acquire_orders()

    assert session_a.account_witness.payload_sha256 == session_b.account_witness.payload_sha256
    assert session_a.provider_account_id == session_b.provider_account_id == "provider-account-A"
    assert session_a.generation_id != session_b.generation_id
    assert session_a.account_context_sha256 != session_b.account_context_sha256
    assert readback_a.evidence_sha256 != readback_b.evidence_sha256

    with pytest.raises(SmarketsSessionContextError, match="not issued by this session"):
        session_b.resolve_orders_readback(readback_a)
    with pytest.raises(SmarketsSessionContextError, match="not issued by this session"):
        session_a.resolve_orders_readback(readback_b)


def test_provider_account_id_changes_context_and_no_caller_label_can_override(monkeypatch) -> None:
    responses = iter(
        (
            _FakeResponse(_account_payload("provider-account-A")),
            _FakeResponse(_account_payload("provider-account-B")),
        )
    )
    monkeypatch.setattr(
        session_context,
        "_open_accounts_request",
        lambda request, timeout: next(responses),
    )
    _install_identity(
        monkeypatch,
        generation_ids=("generation-A", "generation-B"),
        times=(
            "2026-09-22T18:00:00+00:00",
            "2026-09-22T18:00:00+00:00",
        ),
    )

    session_a = open_smarkets_authenticated_session("token-A")
    session_b = open_smarkets_authenticated_session("token-B")

    assert session_a.provider_account_id == "provider-account-A"
    assert session_b.provider_account_id == "provider-account-B"
    assert session_a.account_context_sha256 != session_b.account_context_sha256
    assert "caller_account_id" not in open_smarkets_authenticated_session.__annotations__


def test_importing_private_seal_cannot_make_forged_readback_resolve(monkeypatch) -> None:
    _install_accounts(monkeypatch, _FakeResponse())
    _install_identity(
        monkeypatch,
        times=(
            "2026-09-22T18:00:00+00:00",
            "2026-09-22T18:00:01+00:00",
        ),
    )
    session = open_smarkets_authenticated_session("token-A")
    monkeypatch.setattr(
        orders_acquisition,
        "acquire_smarkets_orders_payload",
        lambda token, *, timeout_seconds: _orders_record(),
    )
    issued = session.acquire_orders()

    forged = SmarketsSessionOrdersReadback(
        session_generation_id=issued.session_generation_id,
        account_context_sha256=issued.account_context_sha256,
        provider_account_id=issued.provider_account_id,
        accounts_payload_sha256=issued.accounts_payload_sha256,
        accounts_available_at=issued.accounts_available_at,
        orders_endpoint=issued.orders_endpoint,
        orders_payload_sha256=issued.orders_payload_sha256,
        orders_payload_size=issued.orders_payload_size,
        orders_provider_date=issued.orders_provider_date,
        orders_received_at=issued.orders_received_at,
        session_bound_at=issued.session_bound_at,
        evidence_sha256=issued.evidence_sha256,
        orders_record=_orders_record(),
        _seal=session_context._READBACK_SEAL,
    )

    with pytest.raises(SmarketsSessionContextError, match="not issued by this session"):
        session.resolve_orders_readback(forged)


def test_close_revokes_session_and_clears_positive_resolution(monkeypatch) -> None:
    _install_accounts(monkeypatch, _FakeResponse())
    _install_identity(
        monkeypatch,
        times=(
            "2026-09-22T18:00:00+00:00",
            "2026-09-22T18:00:01+00:00",
        ),
    )
    session = open_smarkets_authenticated_session("token-A")
    monkeypatch.setattr(
        orders_acquisition,
        "acquire_smarkets_orders_payload",
        lambda token, *, timeout_seconds: _orders_record(),
    )
    readback = session.acquire_orders()

    session.close()

    assert session.closed is True
    assert "token-A" not in repr(session)
    with pytest.raises(SmarketsSessionContextError, match="closed"):
        session.acquire_orders()
    with pytest.raises(SmarketsSessionContextError, match="closed"):
        session.resolve_orders_readback(readback)


def test_provider_date_is_metadata_not_product_availability(monkeypatch) -> None:
    response = _FakeResponse(date="Sun, 01 Jan 2023 00:00:00 GMT")
    _install_accounts(monkeypatch, response)
    _install_identity(monkeypatch, times=("2026-09-22T18:00:00+00:00",))

    session = open_smarkets_authenticated_session("token-A")

    assert session.account_witness.provider_date == "2023-01-01T00:00:00+00:00"
    assert session.account_witness.product_available_at == "2026-09-22T18:00:00+00:00"


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b"null",
        b"123",
        b'"text"',
        b'{"account":{}}',
        b'{"account":{"account_id":"provider-account-A"}}',
        b'{"account":{"account_id":7,"balance":"1","available_balance":"1","exposure":"0","currency":"GBP"}}',
        b'{"account":{"account_id":"provider-account-A","balance":"1","available_balance":"1","exposure":"0","currency":7}}',
    ],
)
def test_non_account_json_does_not_establish_context(monkeypatch, payload: bytes) -> None:
    _install_accounts(monkeypatch, _FakeResponse(payload))

    with pytest.raises(SmarketsSessionContextError, match="provider account"):
        open_smarkets_authenticated_session("token-A")


def test_duplicate_key_payload_error_does_not_leak_provider_controlled_secret(
    monkeypatch,
) -> None:
    secret = "credential-sentinel"
    payload = (
        b'{"credential-sentinel":"one","credential-sentinel":"two"}'
    )
    _install_accounts(monkeypatch, _FakeResponse(payload))

    with pytest.raises(SmarketsSessionContextError) as caught:
        open_smarkets_authenticated_session(secret)

    assert secret not in str(caught.value)
    assert "duplicate" not in str(caught.value).lower()


def test_orders_error_does_not_leak_secret(monkeypatch) -> None:
    secret = "orders-secret-sentinel"
    _install_accounts(monkeypatch, _FakeResponse())
    _install_identity(monkeypatch, times=("2026-09-22T18:00:00+00:00",))
    session = open_smarkets_authenticated_session(secret)

    def fail_orders(token, *, timeout_seconds):
        raise orders_acquisition.SmarketsOrdersAcquisitionError(
            f"provider echoed {secret}"
        )

    monkeypatch.setattr(
        orders_acquisition,
        "acquire_smarkets_orders_payload",
        fail_orders,
    )

    with pytest.raises(SmarketsSessionContextError) as caught:
        session.acquire_orders()

    assert secret not in str(caught.value)


def test_rate_limit_is_unavailable_not_empty_success(monkeypatch) -> None:
    def rate_limited(request, timeout):
        raise HTTPError(
            SMARKETS_ACCOUNTS_ENDPOINT,
            429,
            "Too Many Requests",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(session_context, "_open_accounts_request", rate_limited)

    with pytest.raises(SmarketsSessionContextError, match=r"HTTP 429"):
        open_smarkets_authenticated_session("token-A")


def test_redirected_accounts_response_is_rejected(monkeypatch) -> None:
    response = _FakeResponse(url="https://evil.example/v3/accounts/")
    _install_accounts(monkeypatch, response)

    with pytest.raises(SmarketsSessionContextError, match="fixed official endpoint"):
        open_smarkets_authenticated_session("token-A")


def test_accounts_response_must_be_json(monkeypatch) -> None:
    response = _FakeResponse(content_type="text/html")
    _install_accounts(monkeypatch, response)

    with pytest.raises(SmarketsSessionContextError, match="application/json"):
        open_smarkets_authenticated_session("token-A")


@pytest.mark.parametrize("timeout", [True, 0, 0.09, 60.01, "1"])
def test_timeout_is_bounded(monkeypatch, timeout) -> None:
    _install_accounts(monkeypatch, _FakeResponse())

    with pytest.raises(SmarketsSessionContextError, match="timeout_seconds"):
        open_smarkets_authenticated_session("token-A", timeout_seconds=timeout)


def test_account_and_session_public_constructors_are_not_authority() -> None:
    with pytest.raises(TypeError):
        SmarketsAuthenticatedSession(
            session_token="token",
            generation_id="fake",
            account_witness=object(),
            account_context_sha256="0" * 64,
        )

    with pytest.raises(TypeError):
        session_context.SmarketsAccountReadbackWitness(
            endpoint=SMARKETS_ACCOUNTS_ENDPOINT,
            http_status=200,
            provider_date="2026-09-22T18:00:00+00:00",
            content_type="application/json",
            payload_sha256="0" * 64,
            payload_size=2,
            product_available_at="2026-09-22T18:00:00+00:00",
            provider_account_id="fake-account",
        )


def test_composite_repr_does_not_expose_orders_payload(monkeypatch) -> None:
    secret_in_payload = "provider-payload-credential-sentinel"
    _install_accounts(monkeypatch, _FakeResponse())
    _install_identity(
        monkeypatch,
        times=(
            "2026-09-22T18:00:00+00:00",
            "2026-09-22T18:00:01+00:00",
        ),
    )
    session = open_smarkets_authenticated_session("token-A")
    payload = ('{"orders":[{"note":"%s"}]}' % secret_in_payload).encode()
    monkeypatch.setattr(
        orders_acquisition,
        "acquire_smarkets_orders_payload",
        lambda token, *, timeout_seconds: _orders_record(payload),
    )

    readback = session.acquire_orders()

    assert secret_in_payload not in repr(readback)


def test_incomplete_declared_accounts_body_is_unavailable(monkeypatch) -> None:
    response = _FakeResponse(b'{"account":"short"}')
    response.headers.replace_header("Content-Length", str(len(response.payload) + 10))
    _install_accounts(monkeypatch, response)

    with pytest.raises(SmarketsSessionContextError, match="framing"):
        open_smarkets_authenticated_session("token-A")


def test_orders_readback_is_non_serializable(monkeypatch) -> None:
    _install_accounts(monkeypatch, _FakeResponse())
    _install_identity(
        monkeypatch,
        times=(
            "2026-09-22T18:00:00+00:00",
            "2026-09-22T18:00:01+00:00",
        ),
    )
    session = open_smarkets_authenticated_session("token-A")
    monkeypatch.setattr(
        orders_acquisition,
        "acquire_smarkets_orders_payload",
        lambda token, *, timeout_seconds: _orders_record(),
    )

    readback = session.acquire_orders()

    with pytest.raises(TypeError):
        pickle.dumps(readback)
