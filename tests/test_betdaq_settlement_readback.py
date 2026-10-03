from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from urllib.error import URLError

import pytest

import autosport.betdaq_account_readonly as account_module
import autosport.betdaq_settlement_readback as settlement_module
from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqCredentials,
)
from autosport.betdaq_settlement_readback import (
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
    audit_log="",
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


def postings_window(*rows, complete="true", currency="EUR"):
    return soap(
        "ListAccountPostings",
        (
            f'Currency="{currency}" AvailableFunds="100.00" Balance="120.00" '
            f'Credit="0" Exposure="-20.00" HaveAllPostingsBeenReturned="{complete}"'
        ),
        f"<Orders>{''.join(rows)}</Orders>",
    )


def postings_by_id(*rows, currency="EUR"):
    return soap(
        "ListAccountPostingsById",
        (
            f'Currency="{currency}" AvailableFunds="100.00" Balance="120.00" '
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
    first, _ = economic_client(monkeypatch, payload, clock=clock_one)
    first_value = first.read_account_postings_by_id(9000)
    second, _ = economic_client(monkeypatch, payload, clock=clock_two)
    second_value = second.read_account_postings_by_id(9000)
    assert first_value.evidence.evidence_id == second_value.evidence.evidence_id
    assert first_value.readback_id == second_value.readback_id
    assert (
        first_value.postings[0].observation_id
        == second_value.postings[0].observation_id
    )
    assert first_value.evidence.observed_at != second_value.evidence.observed_at


def test_readback_rejects_row_from_different_acquisition_with_same_evidence_id(
    monkeypatch,
):
    payload = postings_by_id(posting(9001))
    first, _ = economic_client(monkeypatch, payload, clock=clock_one)
    first_value = first.read_account_postings_by_id(9000)
    second, _ = economic_client(monkeypatch, payload, clock=clock_two)
    second_value = second.read_account_postings_by_id(9000)

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
    result = client.read_account_postings_by_id(9000)
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


def test_by_id_rejects_provider_rows_not_strictly_increasing_by_transaction(monkeypatch):
    payload = postings_by_id(
        posting(9002, balance="102.50"),
        posting(9001, amount="-3.00", balance="91.00"),
    )
    client, _ = economic_client(monkeypatch, payload)

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="not strictly increasing by TransactionId",
    ):
        client.read_account_postings_by_id(9000)


def test_by_id_rejects_transaction_at_cursor(monkeypatch):
    client, _ = economic_client(monkeypatch, postings_by_id(posting(9001)))

    with pytest.raises(
        BetdaqEconomicReadbackError,
        match="transaction at/before cursor",
    ):
        client.read_account_postings_by_id(9001)


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
    assert opener.requests == []


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
    assert opener.requests == []


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

    assert len(opener.requests) == 1


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
