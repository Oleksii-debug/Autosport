from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from urllib.error import URLError
import urllib.request as _urllib_request

import pytest

import autosport.betdaq_account_readonly as account_module
import autosport.betdaq_settlement_readback as settlement_module
from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqCredentials,
)
from autosport.betdaq_settlement_readback import (
    BetdaqEconomicEvidence,
    BetdaqEconomicReadbackClient,
    BetdaqEconomicReadbackError,
    coalesce_posting_replays,
)


NS = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


class _FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit=None):
        if limit is None:
            return self.payload
        return self.payload[:limit]

    def info(self):
        # urllib's HTTPErrorProcessor consults response headers even for 2xx.
        return {}


class QueueUrlopen:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        if not self.results:
            raise AssertionError("unexpected HTTPS test dispatch")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return _FakeHttpResponse(result)


def _install_https_test_dispatch(monkeypatch, opener):
    """Intercept below a freshly-built urllib opener without global _opener."""

    def fake_do_open(_self, _http_class, request, **_kwargs):
        response = opener(
            request,
            timeout=getattr(request, "timeout", 0),
        )
        response.code = 200
        response.msg = "OK"
        return response

    monkeypatch.setattr(
        _urllib_request.AbstractHTTPHandler,
        "do_open",
        fake_do_open,
    )


def clock_one():
    return datetime(2026, 9, 23, 0, 10, tzinfo=timezone.utc)


def clock_two():
    return datetime(2026, 9, 23, 0, 20, tzinfo=timezone.utc)


def soap(
    method,
    result_attributes="",
    inner="",
    return_status_code="0",
    *,
    include_return_status=True,
):
    return_status = (
        f'<ReturnStatus Code="{return_status_code}" Description="fixture-status" '
        f'CallId="fixture-call" />'
        if include_return_status
        else ""
    )
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP}" xmlns="{NS}">'
        f"<soap:Body><{method}Response><{method}Result {result_attributes}>"
        f"{return_status}{inner}</{method}Result></{method}Response>"
        f"</soap:Body></soap:Envelope>"
    ).encode()


def order_details(
    *,
    status=4,
    sequence=81,
    settlement=(
        '<OrderSettlementInformation GrossSettlementAmount="12.34" '
        'OrderCommission="0.50" MarketCommission="0.25" '
        'MarketSettledDate="2026-09-22T23:58:00Z" />'
    ),
    audit_log="",
    include_return_status=True,
):
    return soap(
        "GetOrderDetails",
        (
            f'SelectionId="300" OrderStatus="{status}" '
            'IssuedAt="2026-09-22T22:00:00Z" '
            'LastChangedAt="2026-09-22T23:58:01Z" '
            'MarketId="200" RequestedStake="10.00" RequestedPrice="2.50" '
            'TotalStake="10.00" UnmatchedStake="0" AveragePrice="2.45" '
            'MatchingTimeStamp="2026-09-22T22:00:05Z" Polarity="1" '
            f'SequenceNumber="{sequence}" PunterReferenceNumber="77"'
        ),
        settlement + audit_log,
        include_return_status=include_return_status,
    )


def posting(
    transaction_id,
    *,
    amount="2.50",
    balance="102.50",
    category=7,
    order_id="123",
    market_id="200",
    posted_at="2026-09-22T23:59:00Z",
    description="fixture",
):
    optional = ""
    if order_id is not None:
        optional += f' OrderId="{order_id}"'
    if market_id is not None:
        optional += f' MarketId="{market_id}"'
    return (
        f'<Order PostedAt="{posted_at}" Description="{description}" '
        f'Amount="{amount}" ResultingBalance="{balance}" '
        f'PostingCategory="{category}"{optional} TransactionId="{transaction_id}" />'
    )


def postings_window(
    *rows, complete="true", currency="EUR", include_return_status=True
):
    return soap(
        "ListAccountPostings",
        (
            f'Currency="{currency}" AvailableFunds="100.00" Balance="120.00" '
            f'Credit="0" Exposure="-20.00" HaveAllPostingsBeenReturned="{complete}"'
        ),
        f"<Orders>{''.join(rows)}</Orders>",
        include_return_status=include_return_status,
    )


def postings_by_id(*rows, currency="EUR", include_return_status=True):
    return soap(
        "ListAccountPostingsById",
        (
            f'Currency="{currency}" AvailableFunds="100.00" Balance="120.00" '
            'Credit="0" Exposure="-20.00"'
        ),
        f"<Orders>{''.join(rows)}</Orders>",
        include_return_status=include_return_status,
    )


def economic_client(monkeypatch, *responses, clock=clock_one, credentials=None):
    opener = QueueUrlopen(*responses)
    _install_https_test_dispatch(monkeypatch, opener)
    account = BetdaqAccountReadOnlyClient(
        credentials or BetdaqCredentials("alice", "secret-pass", "secret-app"),
        clock=clock,
    )
    return BetdaqEconomicReadbackClient(account), opener


def test_documented_order_audit_log_is_accepted_and_content_bound(monkeypatch):
    first_audit = (
        '<AuditLog><AuditLog Time="2026-09-22T23:58:00Z" '
        'OrderActionType="1" RequestedStake="10.00" TotalStake="10.00" '
        'TotalAgainstStake="4.00" RequestedPrice="2.50" AveragePrice="2.45">'
        '<MatchedOrderInformation xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:nil="true" /><CommissionInformation '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true" />'
        '</AuditLog></AuditLog>'
    )
    second_audit = first_audit.replace(
        'TotalAgainstStake="4.00"',
        'TotalAgainstStake="5.00"',
    )
    client, _ = economic_client(
        monkeypatch,
        order_details(audit_log=first_audit),
        order_details(audit_log=second_audit),
    )

    first = client.read_order_details(123)
    second = client.read_order_details(123)

    assert first.gross_settlement_amount == second.gross_settlement_amount
    assert first.order_commission == second.order_commission
    assert first.evidence.source_payload_sha256 != second.evidence.source_payload_sha256
    assert first.evidence.evidence_id != second.evidence.evidence_id
    assert first.observation_id != second.observation_id


def test_duplicate_order_audit_log_containers_fail_closed(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        order_details(audit_log="<AuditLog /><AuditLog />"),
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        client.read_order_details(123)


def test_order_settlement_preserves_components_without_netting(monkeypatch):
    client, opener = economic_client(monkeypatch, order_details())
    value = client.read_order_details(123)
    assert value.order_id == "123"
    assert value.order_status_code == 4
    assert value.sequence_number == 81
    assert value.gross_settlement_amount == Decimal("12.34")
    assert value.order_commission == Decimal("0.50")
    assert value.market_commission == Decimal("0.25")
    assert value.market_settled_at == "2026-09-22T23:58:00Z"
    assert value.currency is None
    assert value.denomination_proven is False
    assert value.scalar_economic_use_proven is False
    assert value.final_settlement_proven is True
    assert value.evidence.authenticated_principal_continuity_proven is False
    assert value.evidence.physical_account_identity_proven is False
    request, timeout = opener.calls[0]
    assert timeout == 10.0
    soap_action = next(
        value for key, value in request.headers.items() if key.lower() == "soapaction"
    )
    assert soap_action.endswith('/GetOrderDetails"')
    assert b"getOrderDetailsRequest" in request.data
    assert b'OrderId="123"' in request.data


@pytest.mark.parametrize(
    ("commission_attributes", "expected_order_commission", "expected_market_commission"),
    [
        ('OrderCommission="0.50"', Decimal("0.50"), None),
        ('MarketCommission="0.25"', None, Decimal("0.25")),
    ],
)
def test_settled_order_accepts_documented_single_commission_alternative(
    monkeypatch,
    commission_attributes,
    expected_order_commission,
    expected_market_commission,
):
    settlement = (
        '<OrderSettlementInformation GrossSettlementAmount="12.34" '
        f"{commission_attributes} "
        'MarketSettledDate="2026-09-22T23:58:00Z" />'
    )
    client, _ = economic_client(
        monkeypatch,
        order_details(status=4, settlement=settlement),
    )

    value = client.read_order_details(123)

    assert value.gross_settlement_amount == Decimal("12.34")
    assert value.order_commission == expected_order_commission
    assert value.market_commission == expected_market_commission
    assert value.market_settled_at == "2026-09-22T23:58:00Z"
    assert value.final_settlement_proven is True
    assert value.denomination_proven is False
    assert value.scalar_economic_use_proven is False


def test_settled_order_without_either_commission_is_not_final_settlement(
    monkeypatch,
):
    settlement = (
        '<OrderSettlementInformation GrossSettlementAmount="12.34" '
        'MarketSettledDate="2026-09-22T23:58:00Z" />'
    )
    client, _ = economic_client(
        monkeypatch,
        order_details(status=4, settlement=settlement),
    )

    value = client.read_order_details(123)

    assert value.gross_settlement_amount == Decimal("12.34")
    assert value.order_commission is None
    assert value.market_commission is None
    assert value.market_settled_at == "2026-09-22T23:58:00Z"
    assert value.final_settlement_proven is False


def test_settled_current_order_without_settlement_information_is_not_zero_economics(
    monkeypatch,
):
    client, _ = economic_client(monkeypatch, order_details(status=4, settlement=""))
    value = client.read_order_details("123")
    assert value.final_settlement_proven is False
    assert value.gross_settlement_amount is None
    assert value.order_commission is None
    assert value.market_commission is None
    assert value.market_settled_at is None


def test_unknown_order_status_is_preserved_raw_but_cannot_prove_final_settlement(
    monkeypatch,
):
    client, _ = economic_client(monkeypatch, order_details(status=99))
    value = client.read_order_details(123)
    assert value.order_status_code == 99
    assert value.gross_settlement_amount == Decimal("12.34")
    assert value.final_settlement_proven is False


def test_order_settlement_without_provider_currency_cannot_qualify_scalar_economics(
    monkeypatch,
):
    client, _ = economic_client(monkeypatch, order_details())
    value = client.read_order_details(123)
    assert value.final_settlement_proven is True
    assert value.currency is None
    assert value.denomination_proven is False
    assert value.scalar_economic_use_proven is False


def test_incomplete_postings_window_keeps_exact_rows_but_not_complete_absence(
    monkeypatch,
):
    client, _ = economic_client(
        monkeypatch,
        postings_window(posting(9001), complete="false"),
    )
    result = client.read_account_postings(
        datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc),
    )
    assert result.window_complete is False
    assert result.currency == "EUR"
    assert result.balance == Decimal("120.00")
    assert len(result.postings) == 1
    row = result.postings[0]
    assert row.transaction_id == "9001"
    assert row.order_id == "123"
    assert row.market_id == "200"
    assert row.amount == Decimal("2.50")


def test_by_id_read_never_inherits_window_completeness(monkeypatch):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    result = client.read_account_postings_by_id(9000)
    assert result.query_transaction_id == "9000"
    assert result.window_complete is None
    assert len(result.postings) == 1
    body = opener.calls[0][0].data
    assert b"listAccountPostingsByIdRequest" in body
    assert b'TransactionId="9000"' in body


def test_duplicate_transaction_id_is_idempotent_only_for_identical_content(
    monkeypatch,
):
    duplicate = posting(9001)
    client, _ = economic_client(monkeypatch, postings_window(duplicate, duplicate))
    result = client.read_account_postings(
        datetime(2026, 9, 22, tzinfo=timezone.utc),
        datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    assert len(result.postings) == 1
    conflict_client, _ = economic_client(
        monkeypatch,
        postings_window(posting(9001), posting(9001, amount="9.99")),
    )
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="transaction id has conflicting economic content",
    ):
        conflict_client.read_account_postings(
            datetime(2026, 9, 22, tzinfo=timezone.utc),
            datetime(2026, 9, 23, tzinfo=timezone.utc),
        )



def test_same_context_query_and_provider_payload_reresolve_same_evidence_id(monkeypatch):
    payload = postings_by_id(posting(9001))
    client, _ = economic_client(monkeypatch, payload)
    first_value = client.read_account_postings_by_id(9000)

    shifted_evidence = replace(
        first_value.evidence,
        observed_at="2026-09-23T00:20:00Z",
    )
    shifted_postings = tuple(
        replace(item, evidence=shifted_evidence)
        for item in first_value.postings
    )
    second_value = replace(
        first_value,
        evidence=shifted_evidence,
        postings=shifted_postings,
    )

    assert first_value.evidence.evidence_id == second_value.evidence.evidence_id
    assert first_value.readback_id == second_value.readback_id
    assert (
        first_value.postings[0].observation_id
        == second_value.postings[0].observation_id
    )
    assert first_value.evidence.observed_at != second_value.evidence.observed_at
    assert first_value.evidence.acquisition_id != second_value.evidence.acquisition_id

def test_economic_acquisition_identity_binds_product_receive_time(monkeypatch):
    payload = postings_by_id(posting(9001))
    client, _ = economic_client(monkeypatch, payload, clock=clock_one)
    value = client.read_account_postings_by_id(9000)

    shifted = replace(
        value.evidence,
        observed_at="2026-09-23T12:34:56.000000Z",
    )

    assert shifted.evidence_id == value.evidence.evidence_id
    assert shifted.observed_at != value.evidence.observed_at
    assert shifted.acquisition_id != value.evidence.acquisition_id



def test_readback_rejects_row_from_different_acquisition_with_same_evidence_id(
    monkeypatch,
):
    payload = postings_by_id(posting(9001))
    client, _ = economic_client(monkeypatch, payload)
    first_value = client.read_account_postings_by_id(9000)
    second_evidence = replace(
        first_value.evidence,
        observed_at="2026-09-23T00:20:00Z",
    )
    second_postings = tuple(
        replace(item, evidence=second_evidence)
        for item in first_value.postings
    )
    second_value = replace(
        first_value,
        evidence=second_evidence,
        postings=second_postings,
    )

    assert first_value.evidence.evidence_id == second_value.evidence.evidence_id
    assert first_value.evidence != second_value.evidence
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="posting evidence does not match exact readback acquisition",
    ):
        replace(second_value, postings=first_value.postings)

def test_distinct_authenticated_contexts_cannot_collapse_same_economic_payload(
    monkeypatch,
):
    payload = postings_by_id(posting(9001))
    first, _ = economic_client(
        monkeypatch,
        payload,
        credentials=BetdaqCredentials("alice-a", "secret-a", "app-a"),
    )
    first_value = first.read_account_postings_by_id(9000)
    second, _ = economic_client(
        monkeypatch,
        payload,
        credentials=BetdaqCredentials("alice-b", "secret-b", "app-b"),
    )
    second_value = second.read_account_postings_by_id(9000)
    assert first_value.evidence.account_context_id != second_value.evidence.account_context_id
    assert first_value.evidence.evidence_id != second_value.evidence.evidence_id
    assert first_value.readback_id != second_value.readback_id
    assert (
        first_value.postings[0].observation_id
        != second_value.postings[0].observation_id
    )
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="posting evidence does not match exact readback acquisition",
    ):
        replace(second_value, postings=first_value.postings)


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    (
        (
            "_credentials",
            BetdaqCredentials("rotated-user", "rotated-secret", "rotated-app"),
        ),
        ("_venue_id", "rotated-venue"),
    ),
)
def test_economic_read_rejects_authenticated_context_rotation_during_dispatch(
    monkeypatch,
    attribute,
    replacement,
):
    payload = postings_by_id(posting(9001))
    account = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "secret-pass", "secret-app"),
        clock=clock_one,
    )

    class RotatingUrlopen:
        def __init__(self):
            self.calls = []

        def __call__(self, request, *, timeout):
            self.calls.append((request, timeout))
            setattr(account, attribute, replacement)
            return _FakeHttpResponse(payload)

    opener = RotatingUrlopen()
    _install_https_test_dispatch(monkeypatch, opener)
    client = BetdaqEconomicReadbackClient(account)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="authenticated account context changed during economic acquisition",
    ):
        client.read_account_postings_by_id(9000)

    assert len(opener.calls) == 1



def test_economic_read_ignores_preconfigured_caller_clock(monkeypatch):
    payload = postings_by_id(posting(9001))
    caller_clock_calls = []

    def caller_clock():
        caller_clock_calls.append(True)
        return datetime(2001, 1, 1, tzinfo=timezone.utc)

    account = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "secret-pass", "secret-app"),
        clock=caller_clock,
    )
    opener = QueueUrlopen(payload)
    _install_https_test_dispatch(monkeypatch, opener)
    client = BetdaqEconomicReadbackClient(account)

    value = client.read_account_postings_by_id(9000)

    assert len(opener.calls) == 1
    assert caller_clock_calls == []
    assert value.evidence.observed_at != "2001-01-01T00:00:00Z"


def test_economic_read_ignores_account_clock_rotation_during_dispatch(monkeypatch):
    payload = postings_by_id(posting(9001))
    account = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "secret-pass", "secret-app"),
        clock=clock_one,
    )
    hostile_calls = []

    def hostile_clock():
        hostile_calls.append(True)
        raise AssertionError("rotated caller clock executed")

    class RotatingClockUrlopen:
        def __init__(self):
            self.calls = []

        def __call__(self, request, *, timeout):
            self.calls.append((request, timeout))
            account._clock = hostile_clock
            return _FakeHttpResponse(payload)

    opener = RotatingClockUrlopen()
    _install_https_test_dispatch(monkeypatch, opener)
    client = BetdaqEconomicReadbackClient(account)

    value = client.read_account_postings_by_id(9000)

    assert len(opener.calls) == 1
    assert hostile_calls == []
    assert value.evidence.observed_at != "2026-09-23T00:10:00Z"


def test_economic_read_rejects_product_clock_code_mutation_before_dispatch(
    monkeypatch,
):
    payload = postings_by_id(posting(9001))
    client, opener = economic_client(monkeypatch, payload)
    product_clock = settlement_module._product_receive_time
    original_code = product_clock.__code__

    def hostile_clock(_datetime=None, _utc=None):
        raise AssertionError("mutated product receive clock executed")

    assert len(hostile_clock.__code__.co_freevars) == len(original_code.co_freevars)
    try:
        product_clock.__code__ = hostile_clock.__code__
        with pytest.raises(
            BetdaqEconomicReadbackError,
            match="product clock authority was replaced",
        ):
            client.read_account_postings_by_id(9000)
    finally:
        product_clock.__code__ = original_code

    assert opener.calls == []

def test_economic_private_call_cannot_be_widened_to_provider_write_by_globals(
    monkeypatch,
):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    monkeypatch.setattr(
        settlement_module,
        "_READ_METHODS",
        frozenset({"SubmitOrders"}),
        raising=False,
    )
    monkeypatch.setattr(
        settlement_module,
        "_REQUEST_ELEMENT",
        {"SubmitOrders": "submitOrdersRequest"},
        raising=False,
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="outside economic READ allowlist",
    ):
        client._call("SubmitOrders", {"MarketId": "200"})

    assert opener.calls == []


def test_economic_read_rejects_transient_protocol_rebind_from_call_lock(
    monkeypatch,
):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    canonical_endpoint = settlement_module._CANONICAL_SECURE_ENDPOINT
    entered = []

    class TransientProtocolLock:
        def __enter__(self):
            entered.append(True)
            settlement_module._CANONICAL_SECURE_ENDPOINT = (
                "https://example.invalid/credential-capture"
            )
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            settlement_module._CANONICAL_SECURE_ENDPOINT = canonical_endpoint
            return False

    client._account_client._call_lock = TransientProtocolLock()

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic protocol authority was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert entered == [True]
    assert opener.calls == []
    assert settlement_module._CANONICAL_SECURE_ENDPOINT == canonical_endpoint

def test_economic_read_rejects_protocol_rebind_on_call_lock_exit(monkeypatch):
    foreign_ns = "urn:foreign:betdaq:credential-laundering"
    payload = postings_by_id(posting(9001)).replace(
        NS.encode(),
        foreign_ns.encode(),
    )
    client, opener = economic_client(monkeypatch, payload)
    canonical_ns = settlement_module._CANONICAL_EXTERNAL_NS

    class ExitRebindingProtocolLock:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            settlement_module._CANONICAL_EXTERNAL_NS = foreign_ns
            return False

    client._account_client._call_lock = ExitRebindingProtocolLock()

    try:
        with pytest.raises(
            BetdaqEconomicReadbackError,
            match="canonical BETDAQ economic protocol authority was replaced",
        ):
            client.read_account_postings_by_id(9000)
    finally:
        settlement_module._CANONICAL_EXTERNAL_NS = canonical_ns

    assert len(opener.calls) == 1


@pytest.mark.parametrize(
    ("local_alias", "replacement"),
    (
        ("_CANONICAL_SECURE_ENDPOINT", "https://example.invalid/foreign"),
        ("_CANONICAL_EXTERNAL_NS", "urn:foreign:betdaq"),
        ("_CANONICAL_SOAP11_NS", "urn:foreign:soap11"),
        ("_CANONICAL_SOAP12_NS", "urn:foreign:soap12"),
    ),
)
def test_economic_read_rejects_coordinated_local_protocol_alias_rebind(
    monkeypatch,
    local_alias,
    replacement,
):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    monkeypatch.setattr(settlement_module, local_alias, replacement)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic protocol authority was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert opener.calls == []


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    (
        ("_SECURE_ENDPOINT", "https://example.invalid/foreign"),
        ("_EXTERNAL_NS", "urn:foreign:betdaq"),
        ("_SOAP11_NS", "urn:foreign:soap11"),
        ("_SOAP12_NS", "urn:foreign:soap12"),
    ),
)
def test_economic_read_rejects_protocol_authority_replacement_before_dispatch(
    monkeypatch,
    attribute,
    replacement,
):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    monkeypatch.setattr(account_module, attribute, replacement)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic protocol authority was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert opener.calls == []


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    (
        ("_SECURE_ENDPOINT", "https://example.invalid/foreign"),
        ("_EXTERNAL_NS", "urn:foreign:betdaq"),
        ("_SOAP11_NS", "urn:foreign:soap11"),
        ("_SOAP12_NS", "urn:foreign:soap12"),
    ),
)
def test_economic_read_rejects_protocol_authority_replacement_during_dispatch(
    monkeypatch,
    attribute,
    replacement,
):
    payload = postings_by_id(posting(9001))
    account = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "secret-pass", "secret-app"),
        clock=clock_one,
    )
    canonical_endpoint = account_module._SECURE_ENDPOINT

    class RotatingProtocolUrlopen:
        def __init__(self):
            self.calls = []

        def __call__(self, request, *, timeout):
            self.calls.append((request, timeout))
            monkeypatch.setattr(account_module, attribute, replacement)
            return _FakeHttpResponse(payload)

    opener = RotatingProtocolUrlopen()
    _install_https_test_dispatch(monkeypatch, opener)
    client = BetdaqEconomicReadbackClient(account)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic protocol authority was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert len(opener.calls) == 1
    assert opener.calls[0][0].full_url == canonical_endpoint


@pytest.mark.parametrize(
    "attribute",
    ("_credential_context_binding", "_authenticated_account_context"),
)
def test_economic_read_rejects_account_context_authority_replacement_before_dispatch(
    monkeypatch,
    attribute,
):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    hostile_calls = []

    def hostile(credentials, venue_id):
        hostile_calls.append((credentials, venue_id))
        raise AssertionError("hostile account-context resolver executed")

    monkeypatch.setattr(account_module, attribute, hostile)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ authenticated account context authority was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert hostile_calls == []
    assert opener.calls == []


@pytest.mark.parametrize(
    ("account_live", "local_alias", "local_code_alias"),
    (
        (
            "_credential_context_binding",
            "_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT",
            "_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT_CODE",
        ),
        (
            "_authenticated_account_context",
            "_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT",
            "_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT_CODE",
        ),
    ),
)
def test_economic_read_rejects_coordinated_context_alias_rebind_before_dispatch(
    monkeypatch,
    account_live,
    local_alias,
    local_code_alias,
):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    hostile_calls = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("coordinated hostile economic context executed")

    monkeypatch.setattr(account_module, account_live, hostile)
    monkeypatch.setattr(settlement_module, local_alias, hostile)
    monkeypatch.setattr(
        settlement_module,
        local_code_alias,
        hostile.__code__,
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ authenticated account context authority was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert opener.calls == []
    assert hostile_calls == []


@pytest.mark.parametrize(
    ("target_name", "local_name", "local_code_name"),
    (
        (
            "_CANONICAL_HTTPS_POST",
            "_CANONICAL_ACCOUNT_HTTPS_POST",
            "_CANONICAL_ACCOUNT_HTTPS_POST_CODE",
        ),
        (
            "_require_canonical_account_transport",
            "_REQUIRE_CANONICAL_ACCOUNT_TRANSPORT",
            "_CANONICAL_REQUIRE_ACCOUNT_TRANSPORT_CODE",
        ),
    ),
)
def test_economic_read_rejects_coordinated_transport_alias_rebind_before_dispatch(
    monkeypatch,
    target_name,
    local_name,
    local_code_name,
):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    hostile_calls = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("coordinated hostile economic transport executed")

    monkeypatch.setattr(account_module, target_name, hostile)
    monkeypatch.setattr(settlement_module, local_name, hostile)
    monkeypatch.setattr(
        settlement_module,
        local_code_name,
        hostile.__code__,
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic transport dispatch was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert opener.calls == []
    assert hostile_calls == []


def test_economic_read_rejects_account_context_resolver_code_mutation(
    monkeypatch,
):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    resolver = account_module._authenticated_account_context

    def hostile(credentials, venue_id):
        raise AssertionError("mutated account-context resolver executed")

    monkeypatch.setattr(resolver, "__code__", hostile.__code__)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ authenticated account context authority was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert opener.calls == []


def test_economic_read_rejects_account_context_resolver_replacement_during_dispatch(
    monkeypatch,
):
    payload = postings_by_id(posting(9001))
    account = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "secret-pass", "secret-app"),
        clock=clock_one,
    )
    hostile_calls = []

    def hostile(credentials, venue_id):
        hostile_calls.append((credentials, venue_id))
        raise AssertionError("hostile post-dispatch account-context resolver executed")

    class RotatingUrlopen:
        def __init__(self):
            self.calls = []

        def __call__(self, request, *, timeout):
            self.calls.append((request, timeout))
            monkeypatch.setattr(
                account_module,
                "_authenticated_account_context",
                hostile,
            )
            return _FakeHttpResponse(payload)

    opener = RotatingUrlopen()
    _install_https_test_dispatch(monkeypatch, opener)
    client = BetdaqEconomicReadbackClient(account)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ authenticated account context authority was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert len(opener.calls) == 1
    assert hostile_calls == []


def test_economic_read_rejects_account_context_cache_replacement_during_dispatch(
    monkeypatch,
):
    payload = postings_by_id(posting(9001))
    account = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "secret-pass", "secret-app"),
        clock=clock_one,
    )

    class ResettingUrlopen:
        def __init__(self):
            self.calls = []

        def __call__(self, request, *, timeout):
            self.calls.append((request, timeout))
            monkeypatch.setattr(account_module, "_ACCOUNT_CONTEXTS", {})
            return _FakeHttpResponse(payload)

    opener = ResettingUrlopen()
    _install_https_test_dispatch(monkeypatch, opener)
    client = BetdaqEconomicReadbackClient(account)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="authenticated account context changed during economic acquisition",
    ):
        client.read_account_postings_by_id(9000)

    assert len(opener.calls) == 1


def test_transport_exception_is_sanitized(monkeypatch):
    secret = "password=super-secret&applicationIdentifier=private"
    client, _ = economic_client(monkeypatch, URLError(secret))
    with pytest.raises(BetdaqEconomicReadbackError) as raised:
        client.read_account_postings_by_id(9000)
    assert str(raised.value) == "BETDAQ economic read transport failed"
    assert "secret" not in str(raised.value)
    assert "private" not in str(raised.value)


def test_window_bounds_and_completeness_are_strict(monkeypatch):
    client, _ = economic_client(monkeypatch, postings_window(complete="TRUE"))
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="exact provider boolean text",
    ):
        client.read_account_postings(
            datetime(2026, 9, 22, tzinfo=timezone.utc),
            datetime(2026, 9, 23, tzinfo=timezone.utc),
        )
    no_network_client, opener = economic_client(monkeypatch)
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="start must precede end",
    ):
        no_network_client.read_account_postings(
            datetime(2026, 9, 23, tzinfo=timezone.utc),
            datetime(2026, 9, 22, tzinfo=timezone.utc),
        )
    assert opener.calls == []


def test_by_id_response_cannot_mint_window_completeness(monkeypatch):
    payload = soap(
        "ListAccountPostingsById",
        (
            'Currency="EUR" AvailableFunds="100" Balance="120" Credit="0" '
            'Exposure="-20" HaveAllPostingsBeenReturned="true"'
        ),
        f"<Orders>{posting(9001)}</Orders>",
    )
    client, _ = economic_client(monkeypatch, payload)
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="cannot.*window completeness|unexpectedly tries to mint window completeness",
    ):
        client.read_account_postings_by_id(9000)


@pytest.mark.parametrize(
    "read_kind",
    ("order_details", "postings_window", "postings_by_id"),
)
def test_missing_return_status_fails_closed_for_all_economic_reads(
    monkeypatch, read_kind
):
    if read_kind == "order_details":
        payload = order_details(include_return_status=False)
    elif read_kind == "postings_window":
        payload = postings_window(
            posting(9001),
            include_return_status=False,
        )
    else:
        payload = postings_by_id(
            posting(9001),
            include_return_status=False,
        )

    client, _ = economic_client(monkeypatch, payload)
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        if read_kind == "order_details":
            client.read_order_details(123)
        elif read_kind == "postings_window":
            client.read_account_postings(
                datetime(2026, 9, 22, 23, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 23, 0, 10, tzinfo=timezone.utc),
            )
        else:
            client.read_account_postings_by_id(9000)


def test_present_nonzero_return_status_fails_closed(monkeypatch):
    payload = soap(
        "ListAccountPostingsById",
        (
            'Currency="EUR" AvailableFunds="100.00" Balance="120.00" '
            'Credit="0" Exposure="-20.00"'
        ),
        f"<Orders>{posting(9001)}</Orders>",
        return_status_code="17",
    )
    client, _ = economic_client(monkeypatch, payload)
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        client.read_account_postings_by_id(9000)


def test_xsd_decimal_exponent_is_rejected(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        postings_by_id(posting(9001, amount="1E+3")),
    )
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical provider xsd:decimal",
    ):
        client.read_account_postings_by_id(9000)


@pytest.mark.parametrize("currency", ("eur", "EURO", "E1R", " EUR"))
def test_postings_reject_noncanonical_provider_currency(monkeypatch, currency):
    client, _ = economic_client(
        monkeypatch,
        postings_by_id(posting(9001), currency=currency),
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical 3-letter uppercase provider currency",
    ):
        client.read_account_postings_by_id(9000)


@pytest.mark.parametrize(
    "transaction_id",
    (
        9_223_372_036_854_775_808,
        "9223372036854775808",
    ),
)
def test_by_id_request_rejects_value_above_provider_xsd_long_before_transport(
    monkeypatch,
    transaction_id,
):
    client, opener = economic_client(monkeypatch)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="must fit non-negative provider xsd:long",
    ):
        client.read_account_postings_by_id(transaction_id)

    assert opener.calls == []


def test_posting_response_rejects_transaction_id_above_provider_xsd_long(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        postings_by_id(posting("9223372036854775808")),
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="must fit non-negative provider xsd:long",
    ):
        client.read_account_postings_by_id(0)


def test_unsigned_byte_fields_reject_out_of_contract_values(monkeypatch):
    client, _ = economic_client(monkeypatch, order_details(status=256))
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="unsigned-byte provider value",
    ):
        client.read_order_details(123)


def test_soap_body_smuggling_is_rejected(monkeypatch):
    payload = postings_by_id(posting(9001)).replace(
        b"<soap:Body>",
        b'<soap:Body><junk xmlns="urn:not-betdaq" />',
        1,
    )
    client, _ = economic_client(monkeypatch, payload)
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        client.read_account_postings_by_id(9000)


def test_postings_result_sibling_smuggling_is_rejected(monkeypatch):
    payload = postings_by_id(posting(9001)).replace(
        b"</ListAccountPostingsByIdResult>",
        (
            f'<Unexpected xmlns="{NS}" />'
            "</ListAccountPostingsByIdResult>"
        ).encode(),
        1,
    )
    client, _ = economic_client(monkeypatch, payload)
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        client.read_account_postings_by_id(9000)


def test_order_result_sibling_smuggling_is_rejected(monkeypatch):
    payload = order_details().replace(
        b"</GetOrderDetailsResult>",
        (
            f'<Unexpected xmlns="{NS}" />'
            "</GetOrderDetailsResult>"
        ).encode(),
        1,
    )
    client, _ = economic_client(monkeypatch, payload)
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        client.read_order_details(123)


def test_return_status_rejects_unknown_provider_attribute(monkeypatch):
    payload = postings_by_id(posting(9001)).replace(
        b'<ReturnStatus Code="0" ',
        b'<ReturnStatus FutureStatusField="unexpected" Code="0" ',
        1,
    )
    client, _ = economic_client(monkeypatch, payload)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        client.read_account_postings_by_id(9000)


def test_postings_orders_container_rejects_unknown_provider_attribute(monkeypatch):
    payload = postings_by_id(posting(9001)).replace(
        b"<Orders>",
        b'<Orders FutureContainerField="unexpected">',
        1,
    )
    client, _ = economic_client(monkeypatch, payload)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        client.read_account_postings_by_id(9000)


def test_order_result_rejects_unknown_provider_attribute(monkeypatch):
    payload = order_details().replace(
        b"<GetOrderDetailsResult ",
        b'<GetOrderDetailsResult FutureContractField="unexpected" ',
        1,
    )
    client, _ = economic_client(monkeypatch, payload)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        client.read_order_details(123)


def test_order_settlement_rejects_unknown_provider_attribute(monkeypatch):
    payload = order_details().replace(
        b"<OrderSettlementInformation ",
        b'<OrderSettlementInformation FutureSettlementField="unexpected" ',
        1,
    )
    client, _ = economic_client(monkeypatch, payload)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        client.read_order_details(123)


@pytest.mark.parametrize(
    ("payload", "read"),
    (
        (
            postings_window(posting(9001)).replace(
                b"<ListAccountPostingsResult ",
                b'<ListAccountPostingsResult FutureWindowField="unexpected" ',
                1,
            ),
            "window",
        ),
        (
            postings_by_id(posting(9001)).replace(
                b"<ListAccountPostingsByIdResult ",
                b'<ListAccountPostingsByIdResult FutureCursorField="unexpected" ',
                1,
            ),
            "by_id",
        ),
    ),
)
def test_postings_result_rejects_unknown_provider_attribute(
    monkeypatch,
    payload,
    read,
):
    client, _ = economic_client(monkeypatch, payload)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        if read == "window":
            client.read_account_postings(
                datetime(2026, 9, 22, 23, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc),
            )
        else:
            client.read_account_postings_by_id(9000)


def test_posting_row_rejects_unknown_provider_attribute(monkeypatch):
    row = posting(9001).replace(
        "<Order ",
        '<Order FuturePostingField="unexpected" ',
        1,
    )
    client, _ = economic_client(monkeypatch, postings_by_id(row))

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="failed canonical validation",
    ):
        client.read_account_postings_by_id(9000)


def test_order_result_accepts_all_documented_unprojected_attributes(monkeypatch):
    documented = (
        'ExpiresAt="2026-09-23T01:00:00Z" '
        'ValidFrom="2026-09-22T21:59:00Z" '
        'RestrictOrderToBroker="false" '
        'OrderFillType="2" '
        'FillOrKillThreshold="0" '
        'MarketStatus="1" '
        'ExpectedSelectionResetCount="0" '
        'WithdrawlRepriceOption="0" '
        'WithdrawalRepriceOption="0" '
        'CancelOnInRunning="false" '
        'CancelIfSelectionReset="false" '
        'MarketType="1" '
        'ExpectedWithdrawlSequenceNumber="0" '
        'ExpectedWithdrawalSequenceNumber="0" '
    ).encode()
    payload = order_details().replace(
        b"<GetOrderDetailsResult ",
        b"<GetOrderDetailsResult " + documented,
        1,
    )
    client, _ = economic_client(monkeypatch, payload)

    value = client.read_order_details(123)

    assert value.order_id == "123"
    assert value.order_status_code == 4
    assert value.final_settlement_proven is True



class _AdversarialMethod(str):
    def __eq__(self, _other):
        raise AssertionError("method subclass equality must not execute")

    def __hash__(self):
        raise AssertionError("method subclass hash must not execute")


class _AdversarialDecimal(Decimal):
    def is_finite(self):
        raise AssertionError("Decimal subclass hook must not execute")


class _AdversarialDatetime(datetime):
    def isoformat(self, *args, **kwargs):
        raise AssertionError("datetime subclass hook must not execute")

    def astimezone(self, *args, **kwargs):
        raise AssertionError("datetime subclass hook must not execute")


class _ForgedEconomicEvidence:
    evidence_id = "betdaq-economic:" + ("0" * 64)


def test_postings_readback_rejects_method_subclass_before_hash_or_equality(
    monkeypatch,
):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))
    value = client.read_account_postings_by_id(9000)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="postings readback method must be exact text",
    ):
        replace(value, method=_AdversarialMethod("ListAccountPostingsById"))


def test_economic_evidence_rejects_method_subclass_before_equality():
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="economic evidence method must be exact text",
    ):
        BetdaqEconomicEvidence(
            method=_AdversarialMethod("GetOrderDetails"),
            request_identity_sha256="1" * 64,
            source_payload_sha256="2" * 64,
            observed_at="2026-10-01T00:00:00Z",
            account_context_id="betdaq-auth-context:" + ("3" * 64),
        )


def test_order_observation_rejects_forged_evidence_object(monkeypatch):
    client, _ = economic_client(monkeypatch, order_details())
    value = client.read_order_details(123)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="order settlement evidence must be canonical BETDAQ economic evidence",
    ):
        replace(value, evidence=_ForgedEconomicEvidence())


def test_public_economic_dto_rejects_decimal_subclass_before_virtual_dispatch(
    monkeypatch,
):
    client, _ = economic_client(monkeypatch, order_details())
    value = client.read_order_details(123)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="requested_stake must be an exact finite Decimal",
    ):
        replace(value, requested_stake=_AdversarialDecimal("10.00"))


def test_postings_readback_rejects_mutable_container(monkeypatch):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))
    value = client.read_account_postings_by_id(9000)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="postings must be an exact immutable tuple",
    ):
        replace(value, postings=list(value.postings))


def test_identity_timestamp_must_use_canonical_utc_spelling(monkeypatch):
    client, _ = economic_client(monkeypatch, order_details())
    value = client.read_order_details(123)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="issued_at must use canonical UTC timestamp spelling",
    ):
        replace(value, issued_at="2026-09-23T00:00:00+02:00")


def test_request_datetime_subclass_rejected_before_virtual_dispatch(monkeypatch):
    client, opener = economic_client(monkeypatch)
    hostile = _AdversarialDatetime(2026, 9, 22, tzinfo=timezone.utc)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="start_at must be an exact timezone-aware datetime",
    ):
        client.read_account_postings(
            hostile,
            datetime(2026, 9, 23, tzinfo=timezone.utc),
        )

    assert opener.calls == []


def test_repr_never_exposes_credentials(monkeypatch):
    client, _ = economic_client(monkeypatch)
    value = repr(client)
    assert "alice" not in value
    assert "secret-pass" not in value
    assert "secret-app" not in value

def test_posting_identity_survives_window_to_by_id_reresolution(monkeypatch):
    row = posting(9001)
    client, _ = economic_client(
        monkeypatch,
        postings_window(row, complete="false"),
        postings_by_id(row),
    )
    start = datetime(2026, 9, 22, tzinfo=timezone.utc)
    end = datetime(2026, 9, 23, tzinfo=timezone.utc)

    window = client.read_account_postings(start, end)
    by_id = client.read_account_postings_by_id(9000)
    first = window.postings[0]
    repeated = by_id.postings[0]

    assert first.currency == "EUR"
    assert repeated.currency == "EUR"
    assert first.evidence.evidence_id != repeated.evidence.evidence_id
    assert first.canonical_dict() != repeated.canonical_dict()
    assert first.provider_content_dict() == repeated.provider_content_dict()
    assert first.transaction_identity == repeated.transaction_identity
    assert first.observation_id == repeated.observation_id
    assert coalesce_posting_replays(window, by_id) == (first,)


def test_posting_identity_ignores_unrelated_sibling_rows_in_response(monkeypatch):
    row = posting(9001)
    client, _ = economic_client(
        monkeypatch,
        postings_window(row, complete="false"),
        postings_window(
            row,
            posting(9002, balance="105.00"),
            complete="false",
        ),
    )
    start = datetime(2026, 9, 22, tzinfo=timezone.utc)
    end = datetime(2026, 9, 23, tzinfo=timezone.utc)

    first = client.read_account_postings(start, end)
    second = client.read_account_postings(start, end)

    assert first.evidence.evidence_id != second.evidence.evidence_id
    assert (
        first.postings[0].provider_content_dict()
        == second.postings[0].provider_content_dict()
    )
    assert first.postings[0].observation_id == second.postings[0].observation_id
    assert [item.transaction_id for item in coalesce_posting_replays(first, second)] == [
        "9001",
        "9002",
    ]


def test_cross_response_same_transaction_conflict_fails_closed(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        postings_window(posting(9001), complete="false"),
        postings_by_id(posting(9001, amount="9.99")),
    )
    start = datetime(2026, 9, 22, tzinfo=timezone.utc)
    end = datetime(2026, 9, 23, tzinfo=timezone.utc)

    first = client.read_account_postings(start, end)
    conflicting = client.read_account_postings_by_id(9000)

    assert (
        first.postings[0].transaction_identity
        == conflicting.postings[0].transaction_identity
    )
    assert first.postings[0].observation_id != conflicting.postings[0].observation_id
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="transaction id has conflicting economic content",
    ):
        coalesce_posting_replays(first, conflicting)


def test_cross_response_currency_drift_is_economic_conflict(monkeypatch):
    row = posting(9001)
    client, _ = economic_client(
        monkeypatch,
        postings_window(row, complete="false", currency="EUR"),
        postings_by_id(row, currency="USD"),
    )
    start = datetime(2026, 9, 22, tzinfo=timezone.utc)
    end = datetime(2026, 9, 23, tzinfo=timezone.utc)

    euro = client.read_account_postings(start, end)
    usd = client.read_account_postings_by_id(9000)

    assert (
        euro.postings[0].transaction_identity
        == usd.postings[0].transaction_identity
    )
    assert euro.postings[0].currency == "EUR"
    assert usd.postings[0].currency == "USD"
    assert euro.postings[0].observation_id != usd.postings[0].observation_id
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="different provider currencies",
    ):
        coalesce_posting_replays(euro, usd)


@pytest.mark.parametrize("description", [" leading", "trailing ", " both "])
def test_reconstructed_posting_rejects_untrimmed_provider_description(
    monkeypatch,
    description,
):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))
    readback = client.read_account_postings_by_id(9000)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="description must be trimmed provider text",
    ):
        replace(readback.postings[0], description=description)


def test_posting_replay_coalescence_rejects_account_context_mixing(monkeypatch):
    payload = postings_by_id(posting(9001))
    first_client, _ = economic_client(
        monkeypatch,
        payload,
        credentials=BetdaqCredentials("alice-a", "secret-a", "app-a"),
    )
    first = first_client.read_account_postings_by_id(9000)

    second_client, _ = economic_client(
        monkeypatch,
        payload,
        credentials=BetdaqCredentials("alice-b", "secret-b", "app-b"),
    )
    second = second_client.read_account_postings_by_id(9000)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="different authenticated account contexts",
    ):
        coalesce_posting_replays(first, second)


def test_same_credentials_in_fresh_account_context_do_not_mint_restart_identity(
    monkeypatch,
):
    payload = postings_by_id(posting(9001))
    credentials = BetdaqCredentials("alice", "secret-pass", "secret-app")

    first_client, _ = economic_client(
        monkeypatch,
        payload,
        credentials=credentials,
    )
    first = first_client.read_account_postings_by_id(9000)

    monkeypatch.setattr(account_module, "_ACCOUNT_CONTEXTS", {})

    second_client, _ = economic_client(
        monkeypatch,
        payload,
        credentials=credentials,
    )
    second = second_client.read_account_postings_by_id(9000)

    assert first.evidence.account_context_id != second.evidence.account_context_id
    assert (
        first.postings[0].transaction_identity
        != second.postings[0].transaction_identity
    )
    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="different authenticated account contexts",
    ):
        coalesce_posting_replays(first, second)


def test_readback_rejects_posting_currency_mutation(monkeypatch):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))
    readback = client.read_account_postings_by_id(9000)
    forged = replace(readback.postings[0], currency="USD")

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="posting currency does not match readback currency",
    ):
        replace(readback, postings=(forged,))


def test_canonical_readback_rejects_duplicate_transaction_rows(monkeypatch):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))
    readback = client.read_account_postings_by_id(9000)
    row = readback.postings[0]

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="must not retain duplicate transaction ids",
    ):
        replace(readback, postings=(row, row))


def test_by_id_returns_transactions_strictly_after_requested_cursor(monkeypatch):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))

    result = client.read_account_postings_by_id(9000)

    assert result.query_transaction_id == "9000"
    assert [row.transaction_id for row in result.postings] == ["9001"]


@pytest.mark.parametrize(
    ("category", "order_id", "market_id", "message"),
    (
        (1, None, None, "Settlement posting requires exact OrderId"),
        (1, "123", "200", "Settlement posting requires exact OrderId"),
        (2, None, None, "Commission posting requires exact MarketId"),
        (2, "123", "200", "Commission posting requires exact MarketId"),
        (3, "123", None, "Other posting cannot claim"),
        (3, None, "200", "Other posting cannot claim"),
    ),
)
def test_documented_posting_category_handle_contract_fails_closed(
    monkeypatch,
    category,
    order_id,
    market_id,
    message,
):
    client, _ = economic_client(
        monkeypatch,
        postings_by_id(
            posting(
                9001,
                category=category,
                order_id=order_id,
                market_id=market_id,
            )
        ),
    )

    with pytest.raises(BetdaqEconomicReadbackError, match=message):
        client.read_account_postings_by_id(9000)


def test_unknown_future_posting_category_stays_raw(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        postings_by_id(
            posting(
                9001,
                category=7,
                order_id="123",
                market_id="200",
            )
        ),
    )

    row = client.read_account_postings_by_id(9000).postings[0]
    assert row.posting_category == 7
    assert row.order_id == "123"
    assert row.market_id == "200"


def test_window_postings_reject_provider_order_that_moves_backward_in_time(monkeypatch):
    payload = postings_window(
        posting(
            9002,
            balance="102.50",
            posted_at="2026-09-22T23:59:02Z",
        ),
        posting(
            9001,
            amount="-9.00",
            balance="17.00",
            posted_at="2026-09-22T23:59:00Z",
        ),
    )
    client, _ = economic_client(monkeypatch, payload)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="not ordered by increasing PostedAt",
    ):
        client.read_account_postings(
            datetime(2026, 9, 22, tzinfo=timezone.utc),
            datetime(2026, 9, 23, tzinfo=timezone.utc),
        )


def test_by_id_rejects_provider_rows_that_move_backward_by_transaction(monkeypatch):
    payload = postings_by_id(
        posting(9002, balance="102.50"),
        posting(9001, amount="-3.00", balance="91.00"),
    )
    client, _ = economic_client(monkeypatch, payload)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="not ordered ascending by TransactionId",
    ):
        client.read_account_postings_by_id(9000)


def test_by_id_identical_duplicate_transaction_is_idempotent(monkeypatch):
    duplicate = posting(9001)
    client, _ = economic_client(
        monkeypatch,
        postings_by_id(duplicate, duplicate),
    )

    result = client.read_account_postings_by_id(9000)

    assert [row.transaction_id for row in result.postings] == ["9001"]


def test_by_id_rejects_transaction_at_cursor(monkeypatch):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="transaction at/before cursor",
    ):
        client.read_account_postings_by_id(9001)


@pytest.mark.parametrize(
    "posted_at",
    (
        "2026-09-21T23:59:59Z",
        "2026-09-23T00:00:01Z",
    ),
)
def test_window_postings_reject_rows_outside_requested_bounds(
    monkeypatch,
    posted_at,
):
    client, _ = economic_client(
        monkeypatch,
        postings_window(posting(9001, posted_at=posted_at), complete="true"),
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="outside requested window",
    ):
        client.read_account_postings(
            datetime(2026, 9, 22, tzinfo=timezone.utc),
            datetime(2026, 9, 23, tzinfo=timezone.utc),
        )


def test_window_readback_rejects_row_moved_outside_bounds_after_construction(
    monkeypatch,
):
    client, _ = economic_client(
        monkeypatch,
        postings_window(posting(9001), complete="true"),
    )
    readback = client.read_account_postings(
        datetime(2026, 9, 22, tzinfo=timezone.utc),
        datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    forged = replace(
        readback.postings[0],
        posted_at="2026-09-23T00:00:01Z",
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="outside requested window",
    ):
        replace(readback, postings=(forged,))


def test_window_readback_rejects_reordered_rows_after_construction(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        postings_window(
            posting(9001, posted_at="2026-09-22T23:59:00Z"),
            posting(9002, balance="102.50", posted_at="2026-09-22T23:59:01Z"),
            complete="true",
        ),
    )
    readback = client.read_account_postings(
        datetime(2026, 9, 22, tzinfo=timezone.utc),
        datetime(2026, 9, 23, tzinfo=timezone.utc),
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical ListAccountPostings readback is not ordered",
    ):
        replace(readback, postings=tuple(reversed(readback.postings)))


def test_by_id_readback_rejects_reordered_rows_after_construction(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        postings_by_id(
            posting(9001),
            posting(9002, balance="102.50"),
        ),
    )
    readback = client.read_account_postings_by_id(9000)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical ListAccountPostingsById readback is not strictly ordered",
    ):
        replace(readback, postings=tuple(reversed(readback.postings)))


def test_by_id_readback_rejects_row_moved_to_cursor_after_construction(monkeypatch):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))
    readback = client.read_account_postings_by_id(9000)
    forged = replace(readback.postings[0], transaction_id="9000")

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical ListAccountPostingsById readback contains transaction at/before cursor",
    ):
        replace(readback, postings=(forged,))


def test_multiple_settlement_and_commission_postings_are_not_collapsed(monkeypatch):
    payload = postings_by_id(
        posting(
            9001,
            amount="2.50",
            balance="102.50",
            category=1,
            order_id="123",
            market_id=None,
        ),
        posting(
            9002,
            amount="-1.00",
            balance="101.50",
            category=1,
            order_id="123",
            market_id=None,
        ),
        posting(
            9003,
            amount="-0.50",
            balance="101.00",
            category=2,
            order_id=None,
            market_id="200",
        ),
        posting(
            9004,
            amount="-0.25",
            balance="100.75",
            category=2,
            order_id=None,
            market_id="200",
        ),
    )
    client, _ = economic_client(monkeypatch, payload)
    result = client.read_account_postings_by_id(9000)

    assert [row.transaction_id for row in result.postings] == [
        "9001",
        "9002",
        "9003",
        "9004",
    ]
    assert [row.posting_category for row in result.postings] == [1, 1, 2, 2]

def test_description_keywords_cannot_mint_finer_economic_category(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        postings_by_id(
            posting(
                9001,
                category=3,
                order_id=None,
                market_id=None,
                description="commission win deposit settlement",
            )
        ),
    )
    result = client.read_account_postings_by_id(9000)

    row = result.postings[0]
    assert row.posting_category == 3
    assert row.description == "commission win deposit settlement"




def test_order_settlement_rejects_request_identity_laundering(monkeypatch):
    client, _ = economic_client(monkeypatch, order_details())
    value = client.read_order_details(123)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="evidence request identity does not match order_id",
    ):
        replace(value, order_id="124")


def test_window_readback_rejects_request_identity_laundering(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        postings_window(posting(9001), complete="true"),
    )
    readback = client.read_account_postings(
        datetime(2026, 9, 22, tzinfo=timezone.utc),
        datetime(2026, 9, 23, tzinfo=timezone.utc),
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="evidence request identity does not match readback query",
    ):
        replace(readback, query_end_at="2026-09-24T00:00:00Z")


def test_window_readback_rejects_nonincreasing_bounds_after_construction(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        postings_window(posting(9001), complete="true"),
    )
    readback = client.read_account_postings(
        datetime(2026, 9, 22, tzinfo=timezone.utc),
        datetime(2026, 9, 23, tzinfo=timezone.utc),
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="postings window start must precede end",
    ):
        replace(readback, query_start_at=readback.query_end_at)


def test_by_id_readback_rejects_request_identity_laundering(monkeypatch):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))
    readback = client.read_account_postings_by_id(9000)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="evidence request identity does not match readback query",
    ):
        replace(readback, query_transaction_id="9002")


def test_order_settlement_rejects_postings_method_evidence(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        order_details(),
        postings_by_id(posting(9001)),
    )
    order = client.read_order_details(123)
    postings = client.read_account_postings_by_id(9000)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="order settlement evidence method must be GetOrderDetails",
    ):
        replace(order, evidence=postings.evidence)


def test_posting_observation_rejects_order_details_evidence(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        postings_by_id(posting(9001)),
        order_details(),
    )
    postings = client.read_account_postings_by_id(9000)
    order = client.read_order_details(123)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="posting evidence method must be a postings read method",
    ):
        replace(postings.postings[0], evidence=order.evidence)


def test_postings_readback_rejects_cross_method_evidence_laundering(monkeypatch):
    client, _ = economic_client(
        monkeypatch,
        postings_window(posting(9001), complete="true"),
        postings_by_id(posting(9001)),
    )
    start = datetime(2026, 9, 22, tzinfo=timezone.utc)
    end = datetime(2026, 9, 23, tzinfo=timezone.utc)

    window = client.read_account_postings(start, end)
    by_id = client.read_account_postings_by_id(9000)
    forged_row = replace(by_id.postings[0], evidence=window.evidence)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="postings evidence method does not match readback method",
    ):
        replace(by_id, evidence=window.evidence, postings=(forged_row,))


def test_postings_reject_account_currency_validator_replacement_before_execution(
    monkeypatch,
):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))
    hostile_calls = []

    def hostile(value):
        hostile_calls.append(value)
        return value

    monkeypatch.setattr(account_module, "_currency", hostile)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ currency validator was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert hostile_calls == []


def test_postings_reject_account_currency_validator_code_mutation(monkeypatch):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))

    def permissive(value):
        return value

    monkeypatch.setattr(account_module._currency, "__code__", permissive.__code__)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ currency validator was replaced",
    ):
        client.read_account_postings_by_id(9000)


@pytest.mark.parametrize(
    "attribute",
    ("_CANONICAL_ACCOUNT_HTTPS_POST", "_REQUIRE_CANONICAL_ACCOUNT_TRANSPORT"),
)
def test_economic_read_rejects_local_transport_alias_replacement_before_execution(
    monkeypatch,
    attribute,
):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    hostile_calls = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile economic transport alias executed")

    monkeypatch.setattr(settlement_module, attribute, hostile)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic evidence requires product-owned HTTPS transport",
    ):
        client.read_account_postings_by_id(9000)

    assert hostile_calls == []
    assert opener.calls == []


def test_economic_read_rejects_account_transport_class_replacement_before_execution(
    monkeypatch,
):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    hostile_calls = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile account transport class dispatch executed")

    monkeypatch.setattr(account_module.UrllibBetdaqSoapTransport, "post", hostile)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic evidence requires product-owned HTTPS transport",
    ):
        client.read_account_postings_by_id(9000)

    assert hostile_calls == []
    assert opener.calls == []


def test_order_settlement_rejects_local_terminal_status_authority_replacement(
    monkeypatch,
):
    client, opener = economic_client(
        monkeypatch,
        order_details(status=99),
    )
    monkeypatch.setattr(
        settlement_module,
        "_TERMINAL_ORDER_STATUS_CODES",
        frozenset({4, 5, 99}),
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="terminal order status authority was replaced",
    ):
        client.read_order_details(123)

    assert len(opener.calls) == 1


def test_order_dto_rejects_terminal_status_authority_replacement(monkeypatch):
    client, _ = economic_client(monkeypatch, order_details(status=4))
    value = client.read_order_details(123)
    monkeypatch.setattr(
        settlement_module,
        "_TERMINAL_ORDER_STATUS_CODES",
        frozenset({4, 5, 99}),
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="terminal order status authority was replaced",
    ):
        replace(value, order_status_code=99, final_settlement_proven=True)

def test_economic_read_rejects_response_parser_replacement_before_dispatch(
    monkeypatch,
):
    client, opener = economic_client(monkeypatch, postings_by_id(posting(9001)))
    hostile_calls = []

    def hostile(payload, method):
        hostile_calls.append((payload, method))
        raise AssertionError("hostile economic response parser executed")

    monkeypatch.setattr(
        settlement_module,
        "_parse_economic_soap_result",
        hostile,
    )

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic response parser was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert hostile_calls == []
    assert opener.calls == []


def test_economic_read_rejects_response_parser_replacement_during_dispatch(
    monkeypatch,
):
    payload = postings_by_id(posting(9001))
    account = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "secret-pass", "secret-app"),
        clock=clock_one,
    )
    hostile_calls = []

    def hostile(payload, method):
        hostile_calls.append((payload, method))
        raise AssertionError("hostile economic response parser executed")

    class RotatingParserUrlopen:
        def __init__(self):
            self.calls = []

        def __call__(self, request, *, timeout):
            self.calls.append((request, timeout))
            monkeypatch.setattr(
                settlement_module,
                "_parse_economic_soap_result",
                hostile,
            )
            return _FakeHttpResponse(payload)

    opener = RotatingParserUrlopen()
    _install_https_test_dispatch(monkeypatch, opener)
    client = BetdaqEconomicReadbackClient(account)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="canonical BETDAQ economic response parser was replaced",
    ):
        client.read_account_postings_by_id(9000)

    assert len(opener.calls) == 1
    assert hostile_calls == []

