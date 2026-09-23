from datetime import datetime, timezone
from decimal import Decimal
from urllib.error import URLError

import pytest

import autosport.betdaq_account_readonly as account_module
from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqCredentials,
)
from autosport.betdaq_settlement_readback import (
    BetdaqEconomicReadbackClient,
    BetdaqEconomicReadbackError,
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

    def read(self):
        return self.payload


class QueueUrlopen:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        if not self.results:
            raise AssertionError("unexpected urlopen call")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return _FakeHttpResponse(result)


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
        settlement,
    )


def posting(
    transaction_id,
    *,
    amount="2.50",
    balance="102.50",
    category=7,
    order_id="123",
    market_id="200",
):
    optional = ""
    if order_id is not None:
        optional += f' OrderId="{order_id}"'
    if market_id is not None:
        optional += f' MarketId="{market_id}"'
    return (
        f'<Order PostedAt="2026-09-22T23:59:00Z" Description="fixture" '
        f'Amount="{amount}" ResultingBalance="{balance}" '
        f'PostingCategory="{category}"{optional} TransactionId="{transaction_id}" />'
    )


def postings_window(*rows, complete="true"):
    return soap(
        "ListAccountPostings",
        (
            'Currency="EUR" AvailableFunds="100.00" Balance="120.00" '
            f'Credit="0" Exposure="-20.00" HaveAllPostingsBeenReturned="{complete}"'
        ),
        f"<Orders>{''.join(rows)}</Orders>",
    )


def postings_by_id(*rows):
    return soap(
        "ListAccountPostingsById",
        (
            'Currency="EUR" AvailableFunds="100.00" Balance="120.00" '
            'Credit="0" Exposure="-20.00"'
        ),
        f"<Orders>{''.join(rows)}</Orders>",
    )


def economic_client(monkeypatch, *responses, clock=clock_one, credentials=None):
    opener = QueueUrlopen(*responses)
    monkeypatch.setattr(account_module, "urlopen", opener)
    account = BetdaqAccountReadOnlyClient(
        credentials or BetdaqCredentials("alice", "secret-pass", "secret-app"),
        clock=clock,
    )
    return BetdaqEconomicReadbackClient(account), opener


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
    body = request.data
    assert b"getOrderDetailsRequest" in body
    assert b'OrderId="123"' in body


def test_settled_current_order_without_settlement_information_is_not_zero_economics(
    monkeypatch,
):
    client, _ = economic_client(
        monkeypatch,
        order_details(status=4, settlement=""),
    )

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

    result = client.read_account_postings_by_id(9001)

    assert result.query_transaction_id == "9001"
    assert result.window_complete is None
    assert len(result.postings) == 1
    body = opener.calls[0][0].data
    assert b"listAccountPostingsByIdRequest" in body
    assert b'TransactionId="9001"' in body


def test_duplicate_transaction_id_is_idempotent_only_for_identical_content(
    monkeypatch,
):
    duplicate = posting(9001)
    client, _ = economic_client(
        monkeypatch,
        postings_window(duplicate, duplicate),
    )
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
    first, _ = economic_client(monkeypatch, payload, clock=clock_one)
    first_value = first.read_account_postings_by_id(9001)

    second, _ = economic_client(monkeypatch, payload, clock=clock_two)
    second_value = second.read_account_postings_by_id(9001)

    assert first_value.evidence.evidence_id == second_value.evidence.evidence_id
    assert first_value.readback_id == second_value.readback_id
    assert first_value.evidence.observed_at != second_value.evidence.observed_at


def test_distinct_authenticated_contexts_cannot_collapse_same_economic_payload(
    monkeypatch,
):
    payload = postings_by_id(posting(9001))
    first, _ = economic_client(
        monkeypatch,
        payload,
        credentials=BetdaqCredentials("alice-a", "secret-a", "app-a"),
    )
    first_value = first.read_account_postings_by_id(9001)

    second, _ = economic_client(
        monkeypatch,
        payload,
        credentials=BetdaqCredentials("alice-b", "secret-b", "app-b"),
    )
    second_value = second.read_account_postings_by_id(9001)

    assert first_value.evidence.account_context_id != second_value.evidence.account_context_id
    assert first_value.evidence.evidence_id != second_value.evidence.evidence_id
    assert first_value.readback_id != second_value.readback_id


def test_transport_exception_is_sanitized(monkeypatch):
    secret = "password=super-secret&applicationIdentifier=private"
    client, _ = economic_client(monkeypatch, URLError(secret))

    with pytest.raises(BetdaqEconomicReadbackError) as raised:
        client.read_account_postings_by_id(9001)

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
        client.read_account_postings_by_id(9001)


def test_official_generated_response_without_return_status_is_accepted(monkeypatch):
    payload = soap(
        "ListAccountPostingsById",
        (
            'Currency="EUR" AvailableFunds="100.00" Balance="120.00" '
            'Credit="0" Exposure="-20.00"'
        ),
        f"<Orders>{posting(9001)}</Orders>",
        include_return_status=False,
    )
    client, _ = economic_client(monkeypatch, payload)

    result = client.read_account_postings_by_id(9001)

    assert result.postings[0].transaction_id == "9001"
    assert result.window_complete is None


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
        client.read_account_postings_by_id(9001)


def test_repr_never_exposes_credentials(monkeypatch):
    client, _ = economic_client(monkeypatch)
    value = repr(client)
    assert "alice" not in value
    assert "secret-pass" not in value
    assert "secret-app" not in value
