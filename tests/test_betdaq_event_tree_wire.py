from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betdaq_event_tree_wire import (
    parse_get_event_subtree_no_selections_response,
    parse_list_top_level_events_response,
)
from autosport.betdaq_readonly_market_wire import (
    BetdaqProviderStatusError,
    BetdaqSoapFaultError,
    BetdaqSoapProtocolError,
)


API = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"


def _market(*, market_id: int = 9001, event_id: int = 101, status: int = 2) -> str:
    return f"""
      <Markets
        Id="{market_id}"
        Name="Match Winner"
        Type="1"
        IsPlayMarket="false"
        Status="{status}"
        NumberOfWinningSelections="1"
        StartTime="2026-09-23T12:00:00Z"
        WithdrawalSequenceNumber="7"
        DisplayOrder="1"
        IsEnabledForMultiples="true"
        IsInRunningAllowed="true"
        IsManagedWhenInRunning="true"
        IsCurrentlyInRunning="false"
        InRunningDelaySeconds="5"
        EventClassifierId="{event_id}"
        RaceGrade="Grade 1"
        PlacePayout="0.5000">
        <Selections xsi:nil="true" />
      </Markets>
    """


def _tree() -> str:
    return f"""
      <EventClassifiers
        Id="100"
        Name="Soccer"
        DisplayOrder="1"
        IsEnabledForMultiples="true"
        ParentId="0">
        <EventClassifiers
          Id="101"
          Name="Fixture A"
          DisplayOrder="2"
          IsEnabledForMultiples="true"
          ParentId="100">
          {_market()}
        </EventClassifiers>
      </EventClassifiers>
    """


def _response(
    *,
    operation: str = "GetEventSubTreeNoSelections",
    soap_ns: str = SOAP11,
    tree: str | None = None,
    return_status: str = "",
    result_extra: str = "",
    header_extra: str = "",
) -> str:
    content = _tree() if tree is None else tree
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="{soap_ns}" xmlns:xsi="{XSI}">
  {header_extra}
  <soap:Body>
    <{operation}Response xmlns="{API}">
      <{operation}Result>
        {return_status}
        {content}
        {result_extra}
      </{operation}Result>
    </{operation}Response>
  </soap:Body>
</soap:Envelope>"""


def test_subtree_preserves_provider_native_hierarchy_and_market_metadata() -> None:
    response = parse_get_event_subtree_no_selections_response(_response())

    assert response.operation == "GetEventSubTreeNoSelections"
    assert response.return_status_present is False
    assert response.return_code is None
    assert response.return_description is None
    assert response.call_id is None
    assert response.provider_created_at is None
    assert len(response.event_classifiers) == 1

    sport = response.event_classifiers[0]
    assert sport.event_classifier_id == 100
    assert sport.parent_id == 0
    assert sport.name == "Soccer"
    assert sport.is_enabled_for_multiples is True
    assert len(sport.children) == 1

    fixture = sport.children[0]
    assert fixture.event_classifier_id == 101
    assert fixture.parent_id == 100
    assert fixture.markets[0].market_id == 9001
    assert fixture.markets[0].event_classifier_id == 101
    assert fixture.markets[0].status_code == 2
    assert fixture.markets[0].withdrawal_sequence_number == 7
    assert fixture.markets[0].is_currently_in_running is False
    assert fixture.markets[0].in_running_delay_seconds == 5
    assert fixture.markets[0].start_time_text == "2026-09-23T12:00:00Z"
    assert fixture.markets[0].race_grade == "Grade 1"
    assert fixture.markets[0].place_payout == Decimal("0.5000")


def test_absent_return_status_is_not_invented_as_success() -> None:
    response = parse_get_event_subtree_no_selections_response(_response())
    assert response.return_status_present is False
    assert response.return_code is None


def test_present_success_return_status_is_preserved() -> None:
    status = '<ReturnStatus Code="0" Description="Success" CallId="call-7" />'
    response = parse_get_event_subtree_no_selections_response(
        _response(return_status=status)
    )

    assert response.return_status_present is True
    assert response.return_code == 0
    assert response.return_description == "Success"
    assert response.call_id == "call-7"


def test_result_and_return_status_unknown_attributes_fail_closed() -> None:
    result_attribute = _response().replace(
        "<GetEventSubTreeNoSelectionsResult>",
        '<GetEventSubTreeNoSelectionsResult FutureSemanticField="x">',
        1,
    )
    with pytest.raises(BetdaqSoapProtocolError, match="attributes"):
        parse_get_event_subtree_no_selections_response(result_attribute)

    status = (
        '<ReturnStatus Code="0" Description="Success" CallId="call-7" '
        'FutureSemanticField="x" />'
    )
    with pytest.raises(BetdaqSoapProtocolError, match="attributes"):
        parse_get_event_subtree_no_selections_response(
            _response(return_status=status)
        )


def test_event_and_market_unknown_attributes_fail_closed() -> None:
    event_attribute = _response().replace(
        'ParentId="0">',
        'ParentId="0" FutureSemanticField="x">',
        1,
    )
    with pytest.raises(BetdaqSoapProtocolError, match="unexpected provider attribute"):
        parse_get_event_subtree_no_selections_response(event_attribute)

    market_attribute = _response().replace(
        'PlacePayout="0.5000">',
        'PlacePayout="0.5000" FutureSemanticField="x">',
        1,
    )
    with pytest.raises(BetdaqSoapProtocolError, match="unexpected provider attribute"):
        parse_get_event_subtree_no_selections_response(market_attribute)


def test_non_success_return_status_fails_before_tree_publication() -> None:
    status = (
        '<ReturnStatus Code="533" '
        'Description="PunterNotAuthorisedForAPI" CallId="call-8" />'
    )
    with pytest.raises(BetdaqProviderStatusError) as exc:
        parse_get_event_subtree_no_selections_response(
            _response(return_status=status)
        )

    assert exc.value.code == 533
    assert exc.value.call_id == "call-8"


def test_list_top_level_events_uses_same_strict_tree_contract() -> None:
    tree = """
      <EventClassifiers
        Id="10"
        Name="Horse Racing"
        DisplayOrder="1"
        IsEnabledForMultiples="false"
        ParentId="0">
        <Markets xsi:nil="true" />
        <EventClassifiers xsi:nil="true" />
      </EventClassifiers>
    """
    response = parse_list_top_level_events_response(
        _response(operation="ListTopLevelEvents", tree=tree)
    )

    assert response.operation == "ListTopLevelEvents"
    assert response.event_classifiers[0].event_classifier_id == 10
    assert response.event_classifiers[0].markets == ()
    assert response.event_classifiers[0].children == ()


def test_provider_security_created_is_observed_without_local_synthesis() -> None:
    header = f"""<soap:Header xmlns:soap="{SOAP11}" xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}">
      <wsse:Security>
        <wsu:Timestamp>
          <wsu:Created>2026-09-23T00:59:01.123Z</wsu:Created>
        </wsu:Timestamp>
      </wsse:Security>
    </soap:Header>"""
    response = parse_get_event_subtree_no_selections_response(
        _response(header_extra=header)
    )

    assert response.provider_created_at_text == "2026-09-23T00:59:01.123Z"
    assert response.provider_created_at is not None
    assert response.provider_created_at.utcoffset().total_seconds() == 0


def test_nested_parent_identity_mismatch_fails_closed() -> None:
    payload = _response().replace('ParentId="100"', 'ParentId="999"', 1)
    with pytest.raises(BetdaqSoapProtocolError, match="ParentId"):
        parse_get_event_subtree_no_selections_response(payload)


def test_market_event_classifier_identity_mismatch_fails_closed() -> None:
    payload = _response().replace('EventClassifierId="101"', 'EventClassifierId="999"')
    with pytest.raises(BetdaqSoapProtocolError, match="EventClassifierId"):
        parse_get_event_subtree_no_selections_response(payload)


def test_no_selections_response_rejects_real_selection_payload() -> None:
    payload = _response().replace(
        '<Selections xsi:nil="true" />',
        '<Selections Id="55" Name="Should not be here" />',
    )
    with pytest.raises(BetdaqSoapProtocolError, match="must not carry selection data"):
        parse_get_event_subtree_no_selections_response(payload)


def test_nil_placeholder_cannot_hide_provider_attributes_or_children() -> None:
    tree = """
      <EventClassifiers
        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:nil="true"
        Id="1" />
    """
    with pytest.raises(BetdaqSoapProtocolError, match="nil placeholder"):
        parse_get_event_subtree_no_selections_response(
            _response(tree=tree)
        )


def test_duplicate_event_or_market_ids_are_rejected_globally() -> None:
    duplicate_event = _tree() + _tree()
    with pytest.raises(BetdaqSoapProtocolError, match="duplicate EventClassifier"):
        parse_get_event_subtree_no_selections_response(
            _response(tree=duplicate_event)
        )

    duplicate_market = _market(market_id=9001) + _market(market_id=9001)
    tree = f"""
      <EventClassifiers
        Id="101"
        Name="Fixture"
        DisplayOrder="1"
        IsEnabledForMultiples="true"
        ParentId="0">
        {duplicate_market}
      </EventClassifiers>
    """
    with pytest.raises(BetdaqSoapProtocolError, match="duplicate Market"):
        parse_get_event_subtree_no_selections_response(
            _response(tree=tree)
        )


def test_unknown_numeric_status_is_preserved_not_relabelled() -> None:
    response = parse_get_event_subtree_no_selections_response(
        _response(tree=f"""
          <EventClassifiers
            Id="101"
            Name="Fixture"
            DisplayOrder="1"
            IsEnabledForMultiples="true"
            ParentId="0">
            {_market(status=32767)}
          </EventClassifiers>
        """)
    )
    assert response.event_classifiers[0].markets[0].status_code == 32767


def test_place_payout_uses_xml_schema_decimal_lexical_grammar() -> None:
    for value in ("5E-1", "5e-1", "0_5", "0.5_0"):
        with pytest.raises(BetdaqSoapProtocolError, match="XML Schema decimal"):
            parse_get_event_subtree_no_selections_response(
                _response().replace(
                    'PlacePayout="0.5000"',
                    f'PlacePayout="{value}"',
                )
            )

    for value, expected in (
        ("+0.5000", Decimal("0.5000")),
        (".5", Decimal("0.5")),
        ("0.", Decimal("0")),
        ("000.5000", Decimal("0.5000")),
    ):
        response = parse_get_event_subtree_no_selections_response(
            _response().replace(
                'PlacePayout="0.5000"',
                f'PlacePayout="{value}"',
            )
        )
        assert response.event_classifiers[0].children[0].markets[0].place_payout == expected


def test_nonfinite_or_negative_place_payout_fails_closed() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="finite"):
        parse_get_event_subtree_no_selections_response(
            _response().replace('PlacePayout="0.5000"', 'PlacePayout="NaN"')
        )
    with pytest.raises(BetdaqSoapProtocolError, match="non-negative"):
        parse_get_event_subtree_no_selections_response(
            _response().replace('PlacePayout="0.5000"', 'PlacePayout="-1"')
        )


def test_missing_required_attribute_or_wrong_casing_fails_closed() -> None:
    payload = _response().replace(' WithdrawalSequenceNumber="7"', "", 1)
    with pytest.raises(BetdaqSoapProtocolError, match="WithdrawalSequenceNumber"):
        parse_get_event_subtree_no_selections_response(payload)

    payload = _response().replace('Status="2"', 'status="2"', 1)
    with pytest.raises(BetdaqSoapProtocolError, match="Status"):
        parse_get_event_subtree_no_selections_response(payload)


def test_soap12_and_soap_fault_boundaries_are_shared_with_getprices_wire() -> None:
    response = parse_get_event_subtree_no_selections_response(
        _response(soap_ns=SOAP12)
    )
    assert response.event_classifiers[0].event_classifier_id == 100

    fault = f"""<soap:Envelope xmlns:soap="{SOAP11}">
      <soap:Body>
        <soap:Fault>
          <faultcode>soap:Server</faultcode>
          <faultstring>provider unavailable</faultstring>
        </soap:Fault>
      </soap:Body>
    </soap:Envelope>"""
    with pytest.raises(BetdaqSoapFaultError):
        parse_get_event_subtree_no_selections_response(fault)


def test_wrong_namespace_or_unexpected_result_child_cannot_leak_partial_tree() -> None:
    wrong_ns = _response().replace(API, API.lower())
    with pytest.raises(BetdaqSoapProtocolError, match="Response"):
        parse_get_event_subtree_no_selections_response(wrong_ns)

    with pytest.raises(BetdaqSoapProtocolError, match="unexpected child"):
        parse_get_event_subtree_no_selections_response(
            _response(result_extra="<Unexpected />")
        )


def test_multiple_body_payloads_are_rejected() -> None:
    payload = _response().replace(
        "</soap:Body>",
        f'<OtherResponse xmlns="{API}" /></soap:Body>',
    )
    with pytest.raises(BetdaqSoapProtocolError, match="exactly one"):
        parse_get_event_subtree_no_selections_response(payload)


def test_dtd_and_entity_declarations_are_forbidden() -> None:
    payload = '<!DOCTYPE x [<!ENTITY boom "boom">]>' + _response()
    with pytest.raises(BetdaqSoapProtocolError, match="DTD/entity"):
        parse_get_event_subtree_no_selections_response(payload)


def test_tree_depth_is_bounded() -> None:
    nested = '<EventClassifiers xsi:nil="true" />'
    for depth in reversed(range(67)):
        parent_id = 0 if depth == 0 else 1000 + depth - 1
        nested = (
            f'<EventClassifiers Id="{1000 + depth}" '
            f'Name="L{depth}" DisplayOrder="{depth}" '
            f'IsEnabledForMultiples="false" ParentId="{parent_id}">'
            f'{nested}</EventClassifiers>'
        )

    with pytest.raises(BetdaqSoapProtocolError, match="maximum depth"):
        parse_get_event_subtree_no_selections_response(
            _response(tree=nested)
        )
