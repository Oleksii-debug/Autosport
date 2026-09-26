from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betdaq_readonly_market_wire import (
    BetdaqProviderStatusError,
    BetdaqSoapFaultError,
    BetdaqSoapProtocolError,
    parse_get_prices_response,
)


API = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"


def _response(
    *,
    soap_ns: str = SOAP11,
    return_code: int = 0,
    return_description: str = "Success",
    market_extra: str = "",
    selection_extra: str = "",
    price_nodes: str = (
        '<ForSidePrices Price="2.00" Stake="12.3400" />'
        '<AgainstSidePrices Price="2.10" Stake="9.50" />'
    ),
    result_extra: str = "",
    market_attrs: str = "",
    header_extra: str = "",
) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="{soap_ns}" xmlns:xsi="{XSI}">
  {header_extra}
  <soap:Body>
    <GetPricesResponse xmlns="{API}">
      <GetPricesResult>
        <ReturnStatus Code="{return_code}" Description="{return_description}" CallId="call-123" />
        <MarketPrices
          Id="9001"
          Name="Match Winner – Women"
          Type="1"
          IsPlayMarket="false"
          Status="2"
          NumberOfWinningSelections="1"
          StartTime="2026-09-22T19:00:00Z"
          WithdrawalSequenceNumber="7"
          DisplayOrder="1"
          IsEnabledForMultiples="true"
          IsInRunningAllowed="true"
          IsManagedWhenInRunning="true"
          IsCurrentlyInRunning="false"
          InRunningDelaySeconds="5"
          {market_attrs}>
          <Selections
            Id="501"
            Name="Player A"
            Status="3"
            ResetCount="4"
            DeductionFactor="0.1250"
            {selection_extra}>
            {price_nodes}
          </Selections>
          {market_extra}
        </MarketPrices>
        {result_extra}
      </GetPricesResult>
    </GetPricesResponse>
  </soap:Body>
</soap:Envelope>'''


def test_parses_provider_native_getprices_without_side_canonicalization() -> None:
    response = parse_get_prices_response(_response())

    assert response.return_code == 0
    assert response.return_description == "Success"
    assert response.call_id == "call-123"
    assert response.provider_created_at is None
    assert response.provider_created_at_text is None
    assert len(response.markets) == 1

    market = response.markets[0]
    assert market.market_id == 9001
    assert market.name == "Match Winner – Women"
    assert market.status_code == 2
    assert market.withdrawal_sequence_number == 7
    assert market.is_currently_in_running is False
    assert market.in_running_delay_seconds == 5
    assert market.start_time_text == "2026-09-22T19:00:00Z"
    assert market.start_time.utcoffset().total_seconds() == 0

    selection = market.selections[0]
    assert selection.selection_id == 501
    assert selection.status_code == 3
    assert selection.reset_count == 4
    assert selection.deduction_factor == Decimal("0.1250")
    assert selection.for_side_prices[0].provider_side == "FOR"
    assert selection.for_side_prices[0].price == Decimal("2.00")
    assert selection.for_side_prices[0].stake == Decimal("12.3400")
    assert selection.against_side_prices[0].provider_side == "AGAINST"
    assert selection.against_side_prices[0].price == Decimal("2.10")
    assert selection.against_side_prices[0].stake == Decimal("9.50")



def test_preserves_official_ws_security_created_without_synthesizing_local_time() -> None:
    header = f"""<soap:Header xmlns:soap="{SOAP11}" xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}">
      <wsse:Security>
        <wsu:Timestamp>
          <wsu:Created>2026-09-22T19:17:03.125Z</wsu:Created>
          <wsu:Expires>2026-09-22T19:22:03.125Z</wsu:Expires>
        </wsu:Timestamp>
      </wsse:Security>
    </soap:Header>"""
    response = parse_get_prices_response(_response(header_extra=header))
    assert response.provider_created_at_text == "2026-09-22T19:17:03.125Z"
    assert response.provider_created_at is not None
    assert response.provider_created_at.utcoffset().total_seconds() == 0


def test_malformed_ws_security_created_fails_closed_when_present() -> None:
    header = f"""<soap:Header xmlns:soap="{SOAP11}" xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}">
      <wsse:Security>
        <wsu:Timestamp>
          <wsu:Created>2026-09-22T19:17:03</wsu:Created>
        </wsu:Timestamp>
      </wsse:Security>
    </soap:Header>"""
    with pytest.raises(BetdaqSoapProtocolError, match="timezone"):
        parse_get_prices_response(_response(header_extra=header))

def test_accepts_soap12_envelope() -> None:
    response = parse_get_prices_response(_response(soap_ns=SOAP12))
    assert response.markets[0].market_id == 9001


def test_unknown_numeric_status_is_preserved_not_relabelled() -> None:
    payload = _response().replace('Status="2"', 'Status="32767"', 1)
    response = parse_get_prices_response(payload)
    assert response.markets[0].status_code == 32767


def test_nil_schema_price_nodes_are_absent_not_fake_zero_quotes() -> None:
    payload = _response(
        price_nodes=(
            '<ForSidePrices xsi:nil="true" />'
            '<AgainstSidePrices xsi:nil="1" />'
        )
    )
    selection = parse_get_prices_response(payload).markets[0].selections[0]
    assert selection.for_side_prices == ()
    assert selection.against_side_prices == ()


def test_non_success_base_return_status_fails_closed_before_markets() -> None:
    with pytest.raises(BetdaqProviderStatusError) as exc:
        parse_get_prices_response(
            _response(return_code=533, return_description="PunterNotAuthorisedForAPI")
        )
    assert exc.value.code == 533
    assert exc.value.call_id == "call-123"
    assert exc.value.scope == "response"


def test_rc016_unavailable_market_preserves_valid_sibling_market() -> None:
    unavailable = f'''<MarketPrices xmlns="{API}" Id="9002" ReturnCode="16" />'''
    response = parse_get_prices_response(_response(result_extra=unavailable))

    assert [market.market_id for market in response.markets] == [9001]
    assert len(response.unavailable_markets) == 1
    assert response.unavailable_markets[0].market_id == 9002
    assert response.unavailable_markets[0].return_code == 16


def test_non_rc016_market_level_return_code_still_fails_whole_response() -> None:
    payload = _response(market_attrs='ReturnCode="17"')
    with pytest.raises(BetdaqProviderStatusError) as exc:
        parse_get_prices_response(payload)
    assert exc.value.code == 17
    assert exc.value.scope == "market 9001"


def test_rc016_cannot_carry_partial_price_children() -> None:
    payload = _response(market_attrs='ReturnCode="16"')
    with pytest.raises(BetdaqSoapProtocolError, match="RC016 unavailable market"):
        parse_get_prices_response(payload)


def test_soap11_fault_is_typed_and_does_not_parse_partial_payload() -> None:
    payload = f'''<soap:Envelope xmlns:soap="{SOAP11}">
      <soap:Body>
        <soap:Fault>
          <faultcode>soap:Server</faultcode>
          <faultstring>provider unavailable</faultstring>
        </soap:Fault>
      </soap:Body>
    </soap:Envelope>'''
    with pytest.raises(BetdaqSoapFaultError) as exc:
        parse_get_prices_response(payload)
    assert exc.value.code == "soap:Server"
    assert exc.value.reason == "provider unavailable"


def test_wrong_external_namespace_is_rejected() -> None:
    payload = _response().replace(API, API.lower())
    with pytest.raises(BetdaqSoapProtocolError, match="GetPricesResponse"):
        parse_get_prices_response(payload)


def test_wrong_wire_element_or_attribute_casing_is_rejected() -> None:
    with pytest.raises(BetdaqSoapProtocolError):
        parse_get_prices_response(
            _response().replace("<GetPricesResponse", "<getPricesResponse", 1)
        )
    with pytest.raises(BetdaqSoapProtocolError, match="unexpected attribute"):
        parse_get_prices_response(
            _response().replace('Price="2.00"', 'price="2.00"', 1)
        )


def test_missing_return_status_is_contract_error() -> None:
    payload = _response()
    start = payload.index("<ReturnStatus")
    end = payload.index("/>", start) + 2
    payload = payload[:start] + payload[end:]
    with pytest.raises(BetdaqSoapProtocolError, match="ReturnStatus"):
        parse_get_prices_response(payload)


def test_nonfinite_or_invalid_economic_fields_fail_closed() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="finite"):
        parse_get_prices_response(
            _response().replace('Price="2.00"', 'Price="NaN"', 1)
        )
    with pytest.raises(BetdaqSoapProtocolError, match="non-negative"):
        parse_get_prices_response(
            _response().replace('Stake="12.3400"', 'Stake="-1"', 1)
        )
    with pytest.raises(BetdaqSoapProtocolError, match="positive"):
        parse_get_prices_response(
            _response().replace('Price="2.00"', 'Price="0"', 1)
        )


def test_multi_level_prices_preserve_documented_provider_competitiveness_order() -> None:
    response = parse_get_prices_response(
        _response(
            price_nodes=(
                '<ForSidePrices Price="3.00" Stake="5" />'
                '<ForSidePrices Price="2.50" Stake="7" />'
                '<AgainstSidePrices Price="2.10" Stake="4" />'
                '<AgainstSidePrices Price="2.20" Stake="6" />'
            )
        )
    )

    selection = response.markets[0].selections[0]
    assert [level.price for level in selection.for_side_prices] == [
        Decimal("3.00"),
        Decimal("2.50"),
    ]
    assert [level.price for level in selection.against_side_prices] == [
        Decimal("2.10"),
        Decimal("2.20"),
    ]


@pytest.mark.parametrize(
    "price_nodes",
    [
        (
            '<ForSidePrices Price="2.00" Stake="5" />'
            '<ForSidePrices Price="2.50" Stake="7" />'
        ),
        (
            '<AgainstSidePrices Price="2.20" Stake="5" />'
            '<AgainstSidePrices Price="2.10" Stake="7" />'
        ),
    ],
)
def test_provider_competitiveness_order_violation_fails_closed(
    price_nodes: str,
) -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="competitiveness order"):
        parse_get_prices_response(_response(price_nodes=price_nodes))


def test_duplicate_price_level_is_rejected_not_double_counted() -> None:
    payload = _response(
        price_nodes=(
            '<ForSidePrices Price="2.00" Stake="12" />'
            '<ForSidePrices Price="2.00" Stake="9" />'
        )
    )
    with pytest.raises(BetdaqSoapProtocolError, match="double-count"):
        parse_get_prices_response(payload)


def test_duplicate_selection_or_market_identity_is_rejected() -> None:
    duplicate_selection = '''<Selections
        xmlns="http://www.GlobalBettingExchange.com/ExternalAPI/"
        Id="501" Name="Duplicate" Status="3" ResetCount="4"
        DeductionFactor="0" />'''
    with pytest.raises(BetdaqSoapProtocolError, match="duplicate selection"):
        parse_get_prices_response(_response(market_extra=duplicate_selection))

    duplicate_market = '''<MarketPrices
        xmlns="http://www.GlobalBettingExchange.com/ExternalAPI/"
        Id="9001" Name="Duplicate" Type="1" IsPlayMarket="false" Status="2"
        StartTime="2026-09-22T19:00:00Z" WithdrawalSequenceNumber="7"
        IsInRunningAllowed="true" IsManagedWhenInRunning="true"
        IsCurrentlyInRunning="false" InRunningDelaySeconds="5" />'''
    with pytest.raises(BetdaqSoapProtocolError, match="duplicate market"):
        parse_get_prices_response(_response(result_extra=duplicate_market))


def test_naive_or_malformed_start_time_is_rejected() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="timezone"):
        parse_get_prices_response(
            _response().replace(
                'StartTime="2026-09-22T19:00:00Z"',
                'StartTime="2026-09-22T19:00:00"',
                1,
            )
        )
    with pytest.raises(BetdaqSoapProtocolError, match="ISO-8601"):
        parse_get_prices_response(
            _response().replace(
                'StartTime="2026-09-22T19:00:00Z"',
                'StartTime="not-a-time"',
                1,
            )
        )




@pytest.mark.parametrize(
    ("needle", "replacement", "message"),
    [
        ('Name="Match Winner – Women"', 'Name="   "', "market Name"),
        ('Name="Player A"', 'Name="\t "', "selection Name"),
        ('Description="Success"', 'Description="   "', "ReturnStatus Description"),
        ('CallId="call-123"', 'CallId="  "', "ReturnStatus CallId"),
    ],
)
def test_whitespace_only_provider_text_fails_closed(
    needle: str,
    replacement: str,
    message: str,
) -> None:
    with pytest.raises(BetdaqSoapProtocolError, match=message):
        parse_get_prices_response(_response().replace(needle, replacement, 1))


def test_dtd_and_entity_declarations_are_forbidden() -> None:
    payload = '<!DOCTYPE x [<!ENTITY boom "boom">]>' + _response()
    with pytest.raises(BetdaqSoapProtocolError, match="DTD/entity"):
        parse_get_prices_response(payload)


def test_unexpected_child_cannot_leak_partial_success() -> None:
    payload = _response(
        price_nodes=(
            '<ForSidePrices Price="2.00" Stake="12" />'
            '<Unexpected Price="2.1" Stake="10" />'
        ),
    )
    with pytest.raises(BetdaqSoapProtocolError, match="unexpected child"):
        parse_get_prices_response(payload)

@pytest.mark.parametrize("provider_side", ["ForSidePrices", "AgainstSidePrices"])
def test_non_nil_price_level_unknown_attributes_fail_closed(provider_side: str) -> None:
    price_nodes = (
        f'<{provider_side} Price="2.00" Stake="12.34" '
        'FutureSemanticField="provider-value" />'
    )
    with pytest.raises(BetdaqSoapProtocolError, match="unexpected attribute"):
        parse_get_prices_response(_response(price_nodes=price_nodes))

def test_result_and_return_status_unknown_attributes_fail_closed() -> None:
    result_extra = _response().replace(
        "<GetPricesResult>",
        '<GetPricesResult FutureSemanticField="provider-value">',
        1,
    )
    with pytest.raises(BetdaqSoapProtocolError, match="must not contain attributes"):
        parse_get_prices_response(result_extra)

    status_extra = _response().replace(
        'CallId="call-123"',
        'CallId="call-123" FutureSemanticField="provider-value"',
        1,
    )
    with pytest.raises(BetdaqSoapProtocolError, match="unexpected attribute"):
        parse_get_prices_response(status_extra)

