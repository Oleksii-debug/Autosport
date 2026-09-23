from __future__ import annotations

from decimal import Decimal
import xml.etree.ElementTree as ET

import pytest

from autosport.betdaq_readonly_market_wire import EXTERNAL_API_NS, SOAP11_NS
from autosport.betdaq_readonly_provider import (
    BETDAQ_GET_PRICES_ENDPOINT,
    BETDAQ_GET_PRICES_SOAP_ACTION,
    BetdaqGetPricesRequest,
)
from autosport.betdaq_readonly_request_wire import (
    BetdaqExternalApiHeader,
    build_get_prices_soap11_request,
)


def _tag(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _header(**overrides) -> BetdaqExternalApiHeader:
    values = {
        "username": "fixture-user<&",
        "password": "fixture-pass<&\"'",
        "application_identifier": "fixture-app<&",
        "language_code": "en",
        "version": Decimal("2.0"),
    }
    values.update(overrides)
    return BetdaqExternalApiHeader(**values)


def _request(**overrides) -> BetdaqGetPricesRequest:
    values = {
        "request_id": 987654321,
        "market_ids": (11, 22, 33),
        "threshold_amount": Decimal("5.2500"),
        "number_for_prices_required": 1,
        "number_against_prices_required": 1,
        "want_market_matched_amount": False,
        "want_selections_matched_amounts": True,
        "want_selection_matched_details": False,
    }
    values.update(overrides)
    return BetdaqGetPricesRequest(**values)


def test_serializes_documented_soap11_getprices_shape_and_headers() -> None:
    header = _header()
    request = _request()
    wire = build_get_prices_soap11_request(header, request)

    assert wire.endpoint == BETDAQ_GET_PRICES_ENDPOINT
    assert wire.headers == {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": f'"{BETDAQ_GET_PRICES_SOAP_ACTION}"',
    }

    root = ET.fromstring(wire.body)
    assert root.tag == _tag(SOAP11_NS, "Envelope")
    soap_header = root.find(_tag(SOAP11_NS, "Header"))
    soap_body = root.find(_tag(SOAP11_NS, "Body"))
    assert soap_header is not None
    assert soap_body is not None

    external = soap_header.find(_tag(EXTERNAL_API_NS, "ExternalApiHeader"))
    assert external is not None
    assert external.attrib == {
        "version": "2.0",
        "languageCode": "en",
        "username": "fixture-user<&",
        "password": "fixture-pass<&\"'",
        "applicationIdentifier": "fixture-app<&",
    }

    method = soap_body.find(_tag(EXTERNAL_API_NS, "GetPrices"))
    assert method is not None
    method_request = method.find(_tag(EXTERNAL_API_NS, "getPricesRequest"))
    assert method_request is not None
    assert method_request.attrib == {
        "ThresholdAmount": "5.2500",
        "NumberForPricesRequired": "1",
        "NumberAgainstPricesRequired": "1",
        "WantMarketMatchedAmount": "false",
        "WantSelectionsMatchedAmounts": "true",
        "WantSelectionMatchedDetails": "false",
    }
    assert [
        element.text
        for element in method_request.findall(_tag(EXTERNAL_API_NS, "MarketIds"))
    ] == ["11", "22", "33"]


def test_internal_request_id_is_not_laundered_into_provider_wire_contract() -> None:
    request = _request(request_id=123456789)
    wire = build_get_prices_soap11_request(_header(), request)
    text = wire.body.decode("utf-8")
    assert "123456789" not in text
    assert "request_id" not in text
    assert "RequestId" not in text


def test_credentials_and_raw_xml_are_redacted_from_repr() -> None:
    header = _header(
        username="never-log-user",
        password="never-log-password",
        application_identifier="never-log-app-id",
    )
    wire = build_get_prices_soap11_request(header, _request())

    header_repr = repr(header)
    wire_repr = repr(wire)
    for secret in ("never-log-user", "never-log-password", "never-log-app-id"):
        assert secret not in header_repr
        assert secret not in wire_repr
    assert "credentials=<redacted>" in header_repr
    assert "body=<redacted " in wire_repr
    assert "GetPrices" not in wire_repr


def test_xml_serializer_escapes_opaque_header_values_without_changing_them() -> None:
    header = _header(
        username="user <&> Ω",
        password="pass <&> \"quoted\" 'single'",
        application_identifier="app <&> Ω",
    )
    wire = build_get_prices_soap11_request(header, _request())
    root = ET.fromstring(wire.body)
    external = root.find(
        f"{_tag(SOAP11_NS, 'Header')}/{_tag(EXTERNAL_API_NS, 'ExternalApiHeader')}"
    )
    assert external is not None
    assert external.attrib["username"] == header.username
    assert external.attrib["password"] == header.password
    assert external.attrib["applicationIdentifier"] == header.application_identifier


def test_header_validation_fails_without_echoing_secret_value() -> None:
    secret = "top-secret\nshould-never-appear"
    with pytest.raises(ValueError) as raised:
        _header(password=secret)
    assert secret not in str(raised.value)
    assert "password" in str(raised.value)


def test_header_rejects_unbounded_values_before_xml_materialization() -> None:
    with pytest.raises(ValueError, match="application_identifier is too long"):
        _header(application_identifier="a" * 4097)
    with pytest.raises(ValueError, match="fixed-point representation exceeds"):
        _header(version=Decimal("1E+1000000"))


def test_serializer_is_deterministic_for_identical_request_inputs() -> None:
    first = build_get_prices_soap11_request(_header(), _request())
    second = build_get_prices_soap11_request(_header(), _request())
    assert first.body == second.body
    assert first.headers == second.headers


def test_request_wire_exposes_no_provider_write_surface() -> None:
    wire = build_get_prices_soap11_request(_header(), _request())
    text = wire.body.decode("utf-8")
    assert "PlaceOrders" not in text
    assert "UpdateOrders" not in text
    assert "Cancel" not in text
    assert "SecureService" not in wire.endpoint
