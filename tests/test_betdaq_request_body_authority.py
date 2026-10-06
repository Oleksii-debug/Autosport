from datetime import datetime, timezone
from urllib import request as urllib_request

import pytest

import autosport.betdaq_settlement_readback as settlement_module
from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqCredentials,
)
from autosport.betdaq_settlement_readback import (
    BetdaqEconomicReadbackClient,
    BetdaqEconomicReadbackError,
)


class _FakeHttpResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit=None):
        return self.payload if limit is None else self.payload[:limit]

    def info(self):
        return {}


class _NoDispatchUrlopen:
    def __init__(self):
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        raise AssertionError("hostile BETDAQ request body reached HTTPS dispatch")


def _economic_client(monkeypatch):
    opener = _NoDispatchUrlopen()

    def fake_do_open(_self, _http_class, request, **_kwargs):
        response = opener(request, timeout=getattr(request, "timeout", 0))
        response.code = 200
        response.msg = "OK"
        return response

    monkeypatch.setattr(
        urllib_request.AbstractHTTPHandler,
        "do_open",
        fake_do_open,
    )
    account = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "secret-pass", "secret-app"),
    )
    return BetdaqEconomicReadbackClient(account), opener


def test_replaced_request_builder_cannot_send_other_order_under_original_identity(
    monkeypatch,
):
    client, opener = _economic_client(monkeypatch)
    original_builder = settlement_module._request_xml

    def hostile_builder(credentials, method, attributes):
        body = original_builder(credentials, method, attributes)
        assert b'OrderId="123"' in body
        return body.replace(b'OrderId="123"', b'OrderId="999"')

    monkeypatch.setattr(settlement_module, "_request_xml", hostile_builder)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="request body does not match exact economic request authority",
    ):
        client.read_order_details(123)

    assert opener.calls == []


def test_request_builder_cannot_mutate_attributes_before_dispatch(monkeypatch):
    client, opener = _economic_client(monkeypatch)
    original_builder = settlement_module._request_xml

    def hostile_builder(credentials, method, attributes):
        attributes["OrderId"] = "999"
        return original_builder(credentials, method, attributes)

    monkeypatch.setattr(settlement_module, "_request_xml", hostile_builder)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="request body does not match exact economic request authority",
    ):
        client.read_order_details(123)

    assert opener.calls == []


def test_by_id_request_body_cannot_drift_from_cursor_identity(monkeypatch):
    client, opener = _economic_client(monkeypatch)
    original_builder = settlement_module._request_xml

    def hostile_builder(credentials, method, attributes):
        body = original_builder(credentials, method, attributes)
        assert b'TransactionId="10"' in body
        return body.replace(b'TransactionId="10"', b'TransactionId="11"')

    monkeypatch.setattr(settlement_module, "_request_xml", hostile_builder)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="request body does not match exact economic request authority",
    ):
        client.read_account_postings_by_id(10)

    assert opener.calls == []


def test_window_request_body_cannot_drift_from_time_identity(monkeypatch):
    client, opener = _economic_client(monkeypatch)
    original_builder = settlement_module._request_xml

    def hostile_builder(credentials, method, attributes):
        body = original_builder(credentials, method, attributes)
        assert method == "ListAccountPostings"
        start = attributes["StartTime"].encode("utf-8")
        end = attributes["EndTime"].encode("utf-8")
        assert start in body
        return body.replace(start, end, 1)

    monkeypatch.setattr(settlement_module, "_request_xml", hostile_builder)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="request body does not match exact economic request authority",
    ):
        client.read_account_postings(
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

    assert opener.calls == []


def test_request_body_method_cannot_drift_from_soap_action(monkeypatch):
    client, opener = _economic_client(monkeypatch)
    original_builder = settlement_module._request_xml

    def hostile_builder(credentials, method, attributes):
        assert method == "GetOrderDetails"
        assert attributes == {"OrderId": "123"}
        return original_builder(
            credentials,
            "ListAccountPostingsById",
            {"TransactionId": "10"},
        )

    monkeypatch.setattr(settlement_module, "_request_xml", hostile_builder)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="request body does not match exact economic request authority",
    ):
        client.read_order_details(123)

    assert opener.calls == []


def test_request_body_credentials_cannot_drift_from_account_context(monkeypatch):
    client, opener = _economic_client(monkeypatch)
    original_builder = settlement_module._request_xml

    def hostile_builder(credentials, method, attributes):
        body = original_builder(credentials, method, attributes)
        assert b'username="alice"' in body
        return body.replace(b'username="alice"', b'username="mallory"', 1)

    monkeypatch.setattr(settlement_module, "_request_xml", hostile_builder)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="request body does not match exact economic request authority",
    ):
        client.read_order_details(123)

    assert opener.calls == []


def test_request_builder_code_identity_drift_is_rejected_before_dispatch(
    monkeypatch,
):
    client, opener = _economic_client(monkeypatch)
    builder = settlement_module._request_xml
    original_code = builder.__code__
    builder.__code__ = original_code.replace(co_name="drifted_betdaq_request_xml")
    try:
        with pytest.raises(
            BetdaqEconomicReadbackError,
            match="request body does not match exact economic request authority",
        ):
            client.read_order_details(123)
    finally:
        builder.__code__ = original_code

    assert opener.calls == []



def test_inflight_request_builder_rebind_is_rejected_before_https_dispatch(
    monkeypatch,
):
    client, opener = _economic_client(monkeypatch)
    account = client._account_client
    original_builder = settlement_module._request_xml
    mutated = False

    def hostile_builder(credentials, method, attributes):
        body = original_builder(credentials, method, attributes)
        return body.replace(b'OrderId="123"', b'OrderId="999"', 1)

    class _MutatingCallLock:
        def __enter__(self):
            nonlocal mutated
            mutated = True
            monkeypatch.setattr(
                settlement_module,
                "_request_xml",
                hostile_builder,
            )
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    account._call_lock = _MutatingCallLock()

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="request body does not match exact economic request authority",
    ):
        client.read_order_details(123)

    assert mutated is True
    assert opener.calls == []

def test_request_builder_et_dependency_rebind_fails_before_hostile_code(
    monkeypatch,
):
    client, opener = _economic_client(monkeypatch)
    hostile_calls = []

    def hostile_subelement(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile XML builder dependency executed")

    monkeypatch.setattr(settlement_module.ET, "SubElement", hostile_subelement)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic XML request authority was replaced",
    ):
        client.read_order_details(123)

    assert hostile_calls == []
    assert opener.calls == []


def test_response_parser_et_dependency_rebind_fails_before_hostile_code(
    monkeypatch,
):
    hostile_calls = []

    def hostile_fromstring(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile XML parser dependency executed")

    monkeypatch.setattr(settlement_module.ET, "fromstring", hostile_fromstring)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic XML response authority was replaced",
    ):
        settlement_module._parse_economic_soap_result(
            b"<provider-bytes-must-not-reach-hostile-parser/>",
            "GetOrderDetails",
        )

    assert hostile_calls == []

