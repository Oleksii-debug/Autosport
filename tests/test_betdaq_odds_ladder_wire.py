from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betdaq_odds_ladder_wire import parse_get_odds_ladder_response
from autosport.betdaq_readonly_market_wire import (
    BetdaqProviderStatusError,
    BetdaqSoapFaultError,
    BetdaqSoapProtocolError,
)


API = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP12 = "http://www.w3.org/2003/05/soap-envelope"


def _response(
    *,
    soap_ns: str = SOAP11,
    entries: str = (
        '<Ladder price="1.50" representation="1/2" />'
        '<Ladder price="2.00" representation="Evens" />'
        '<Ladder price="3.25" representation="9/4" />'
    ),
    return_status: str = "",
    extra: str = "",
) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="{soap_ns}">
  <soap:Body>
    <GetOddsLadderResponse xmlns="{API}">
      <GetOddsLadderResult>
        {return_status}
        {entries}
        {extra}
      </GetOddsLadderResult>
    </GetOddsLadderResponse>
  </soap:Body>
</soap:Envelope>"""


def test_parses_exact_decimal_ladder_without_float_or_sorting() -> None:
    response = parse_get_odds_ladder_response(_response())

    assert response.return_status_present is False
    assert [item.price for item in response.entries] == [
        Decimal("1.50"),
        Decimal("2.00"),
        Decimal("3.25"),
    ]
    assert [item.price_text for item in response.entries] == ["1.50", "2.00", "3.25"]
    assert [item.representation for item in response.entries] == [
        "1/2",
        "Evens",
        "9/4",
    ]
    assert len(response.content_sha256) == 64


def test_same_provider_content_has_deterministic_identity() -> None:
    first = parse_get_odds_ladder_response(_response())
    second = parse_get_odds_ladder_response(_response())
    assert first.content_sha256 == second.content_sha256
    assert first.entries == second.entries

    changed = parse_get_odds_ladder_response(
        _response(
            entries=(
                '<Ladder price="1.50" representation="1/2" />'
                '<Ladder price="2.02" representation="51/50" />'
            )
        )
    )
    assert changed.content_sha256 != first.content_sha256


def test_content_identity_preserves_provider_order() -> None:
    forward = parse_get_odds_ladder_response(_response())
    reverse = parse_get_odds_ladder_response(
        _response(
            entries=(
                '<Ladder price="3.25" representation="9/4" />'
                '<Ladder price="2.00" representation="Evens" />'
                '<Ladder price="1.50" representation="1/2" />'
            )
        )
    )
    assert reverse.content_sha256 != forward.content_sha256
    assert reverse.entries[0].price == Decimal("3.25")


def test_absent_return_status_is_not_fabricated_as_success() -> None:
    response = parse_get_odds_ladder_response(_response())
    assert response.return_status_present is False
    assert response.return_code is None
    assert response.return_description is None
    assert response.call_id is None


def test_present_return_status_is_preserved_and_failure_is_typed() -> None:
    success = parse_get_odds_ladder_response(
        _response(
            return_status='<ReturnStatus Code="0" Description="Success" CallId="x1" />'
        )
    )
    assert success.return_status_present is True
    assert success.return_code == 0
    assert success.call_id == "x1"

    with pytest.raises(BetdaqProviderStatusError) as exc:
        parse_get_odds_ladder_response(
            _response(
                return_status=(
                    '<ReturnStatus Code="533" '
                    'Description="PunterNotAuthorisedForAPI" CallId="x2" />'
                )
            )
        )
    assert exc.value.code == 533
    assert exc.value.call_id == "x2"


def test_duplicate_numeric_price_fails_closed_even_with_different_lexeme() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="duplicate Ladder price"):
        parse_get_odds_ladder_response(
            _response(
                entries=(
                    '<Ladder price="2.00" representation="Evens" />'
                    '<Ladder price="2.0" representation="2 decimal" />'
                )
            )
        )

def test_invalid_or_empty_ladder_cannot_be_current_authority() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="at least one"):
        parse_get_odds_ladder_response(_response(entries=""))

    for price in ("NaN", "Infinity", "1", "0", "-2"):
        with pytest.raises(BetdaqSoapProtocolError):
            parse_get_odds_ladder_response(
                _response(entries=f'<Ladder price="{price}" representation="bad" />')
            )


def test_price_lexeme_must_be_trimmed_and_representation_nonempty() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="trimmed"):
        parse_get_odds_ladder_response(
            _response(entries='<Ladder price=" 2.00 " representation="Evens" />')
        )
    with pytest.raises(BetdaqSoapProtocolError):
        parse_get_odds_ladder_response(
            _response(entries='<Ladder price="2.00" representation="" />')
        )


def test_unknown_ladder_attribute_cannot_alias_content_identity() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="exactly price and representation"):
        parse_get_odds_ladder_response(
            _response(
                entries=(
                    '<Ladder price="2.00" representation="Evens" '
                    'futureSemanticField="provider-value" />'
                )
            )
        )


def test_wrong_namespace_extra_payload_and_child_content_fail_closed() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="Response"):
        parse_get_odds_ladder_response(_response().replace(API, API.lower()))

    with pytest.raises(BetdaqSoapProtocolError, match="unexpected child"):
        parse_get_odds_ladder_response(_response(extra="<Unexpected />"))

    with pytest.raises(BetdaqSoapProtocolError, match="child content"):
        parse_get_odds_ladder_response(
            _response(entries='<Ladder price="2.00" representation="Evens"><x /></Ladder>')
        )


def test_soap12_and_fault_boundaries_are_supported() -> None:
    response = parse_get_odds_ladder_response(_response(soap_ns=SOAP12))
    assert response.entries[0].price == Decimal("1.50")

    fault = f"""<soap:Envelope xmlns:soap="{SOAP11}">
      <soap:Body>
        <soap:Fault>
          <faultcode>soap:Server</faultcode>
          <faultstring>provider unavailable</faultstring>
        </soap:Fault>
      </soap:Body>
    </soap:Envelope>"""
    with pytest.raises(BetdaqSoapFaultError):
        parse_get_odds_ladder_response(fault)


def test_dtd_and_multiple_body_payloads_are_rejected() -> None:
    payload = '<!DOCTYPE x [<!ENTITY boom "boom">]>' + _response()
    with pytest.raises(BetdaqSoapProtocolError, match="DTD/entity"):
        parse_get_odds_ladder_response(payload)

    payload = _response().replace(
        "</soap:Body>",
        f'<OtherResponse xmlns="{API}" /></soap:Body>',
    )
    with pytest.raises(BetdaqSoapProtocolError, match="exactly one"):
        parse_get_odds_ladder_response(payload)
