from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betdaq_selection_sequence_wire import (
    parse_get_current_selection_sequence_number_response,
    parse_list_selections_changed_since_response,
)
from autosport.betdaq_readonly_market_wire import (
    BetdaqProviderStatusError,
    BetdaqSoapFaultError,
    BetdaqSoapProtocolError,
)


API = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP12 = "http://www.w3.org/2003/05/soap-envelope"


def _envelope(operation: str, result: str, *, soap_ns: str = SOAP11) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="{soap_ns}">
  <soap:Body>
    <{operation}Response xmlns="{API}">
      {result}
    </{operation}Response>
  </soap:Body>
</soap:Envelope>"""


def _current(
    sequence: int = 1234,
    *,
    soap_ns: str = SOAP11,
    status: str = "",
) -> str:
    return _envelope(
        "GetCurrentSelectionSequenceNumber",
        (
            f'<GetCurrentSelectionSequenceNumberResult SelectionSequenceNumber="{sequence}">'
            f"{status}"
            "</GetCurrentSelectionSequenceNumberResult>"
        ),
        soap_ns=soap_ns,
    )


def _selection(
    *,
    selection_id: int = 77,
    market_id: int = 9001,
    sequence: int = 1235,
    cancel_time: str = "2026-09-23T01:10:00Z",
    settlement: str = "",
) -> str:
    return f"""
      <Selections
        Id="{selection_id}"
        Name="Home"
        DisplayOrder="3"
        IsHidden="false"
        Status="2"
        ResetCount="4"
        WithdrawalFactor="0.125"
        MarketId="{market_id}"
        SelectionSequenceNumber="{sequence}"
        CancelOrdersTime="{cancel_time}">
        {settlement}
      </Selections>
    """


def _changed(
    rows: str,
    *,
    soap_ns: str = SOAP11,
    status: str = "",
    extra: str = "",
) -> str:
    return _envelope(
        "ListSelectionsChangedSince",
        f"""<ListSelectionsChangedSinceResult>
          {status}
          {rows}
          {extra}
        </ListSelectionsChangedSinceResult>""",
        soap_ns=soap_ns,
    )


def test_current_sequence_parses_provider_max_without_cursor_claim() -> None:
    response = parse_get_current_selection_sequence_number_response(_current())

    assert response.selection_sequence_number == 1234
    assert response.return_status_present is False
    assert response.return_code is None


def test_current_sequence_supports_soap12_and_optional_return_status() -> None:
    status = '<ReturnStatus Code="0" Description="Success" CallId="seq-1" />'
    response = parse_get_current_selection_sequence_number_response(
        _current(999, soap_ns=SOAP12, status=status)
    )

    assert response.selection_sequence_number == 999
    assert response.return_status_present is True
    assert response.return_code == 0
    assert response.call_id == "seq-1"


def test_changed_selection_preserves_reset_withdrawal_sequence_and_settlement() -> None:
    settlement = """
      <SettlementInformation
        SettledTime="2026-09-23T01:12:00+00:00"
        VoidPercentage="0"
        LeftSideFactor="1.0"
        RightSideFactor="0.5"
        SettlementResultString="Win" />
    """
    response = parse_list_selections_changed_since_response(
        _changed(_selection(settlement=settlement))
    )

    assert response.return_status_present is False
    assert len(response.selections) == 1
    item = response.selections[0]
    assert item.selection_id == 77
    assert item.market_id == 9001
    assert item.status_code == 2
    assert item.reset_count == 4
    assert item.withdrawal_factor == Decimal("0.125")
    assert item.selection_sequence_number == 1235
    assert item.cancel_orders_time.timezone_present is True
    assert item.cancel_orders_time.text == "2026-09-23T01:10:00Z"

    settlement_item = item.settlement_information[0]
    assert settlement_item.settled_time.timezone_present is True
    assert settlement_item.void_percentage == Decimal("0")
    assert settlement_item.left_side_factor == Decimal("1.0")
    assert settlement_item.right_side_factor == Decimal("0.5")
    assert settlement_item.settlement_result_string == "Win"


def test_wire_timestamp_preserves_missing_timezone_instead_of_inventing_utc() -> None:
    response = parse_list_selections_changed_since_response(
        _changed(_selection(cancel_time="2026-09-23T01:10:00"))
    )
    timestamp = response.selections[0].cancel_orders_time
    assert timestamp.text == "2026-09-23T01:10:00"
    assert timestamp.timezone_present is False
    assert timestamp.value.tzinfo is None


@pytest.mark.parametrize(
    "invalid",
    (
        "2026-09-23T01:10",
        "2026-09-23T01:10:00,5",
        "2026-09-23T01:10:00+0000",
        "2026-09-23T01:10:00+14:01",
        "2026-09-23T01:10:00+15:00",
        "20260923T011000",
    ),
)
def test_wire_timestamp_rejects_non_xsd_lexical_forms(invalid: str) -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="ISO-8601"):
        parse_list_selections_changed_since_response(
            _changed(_selection(cancel_time=invalid))
        )


@pytest.mark.parametrize(
    ("wire", "timezone_present"),
    (
        ("2026-09-23T01:10:00Z", True),
        ("2026-09-23T01:10:00+00:00", True),
        ("2026-09-23T01:10:00+14:00", True),
        ("2026-09-23T01:10:00.125", False),
    ),
)
def test_wire_timestamp_accepts_supported_xsd_lexical_forms(
    wire: str,
    timezone_present: bool,
) -> None:
    response = parse_list_selections_changed_since_response(
        _changed(_selection(cancel_time=wire))
    )
    timestamp = response.selections[0].cancel_orders_time

    assert timestamp.text == wire
    assert timestamp.timezone_present is timezone_present


def test_settlement_timestamp_uses_same_xsd_lexical_gate() -> None:
    settlement = """
      <SettlementInformation
        SettledTime="2026-09-23T01:12"
        VoidPercentage="0"
        LeftSideFactor="1.0"
        RightSideFactor="0.5"
        SettlementResultString="Win" />
    """
    with pytest.raises(BetdaqSoapProtocolError, match="ISO-8601"):
        parse_list_selections_changed_since_response(
            _changed(_selection(settlement=settlement))
        )


def test_changed_rows_preserve_provider_order_without_undocumented_sorting() -> None:
    rows = (
        _selection(selection_id=1, sequence=500)
        + _selection(selection_id=2, sequence=499)
    )
    response = parse_list_selections_changed_since_response(_changed(rows))
    assert [row.selection_sequence_number for row in response.selections] == [500, 499]


def test_empty_changed_selection_result_is_valid_but_not_completeness_proof() -> None:
    response = parse_list_selections_changed_since_response(_changed(""))
    assert response.selections == ()
    assert response.return_status_present is False


def test_exact_duplicate_selection_sequence_row_fails_closed() -> None:
    row = _selection(selection_id=8, sequence=600)
    with pytest.raises(BetdaqSoapProtocolError, match="duplicate selection/sequence"):
        parse_list_selections_changed_since_response(_changed(row + row))


def test_same_selection_id_with_distinct_sequences_is_preserved() -> None:
    rows = (
        _selection(selection_id=8, sequence=600)
        + _selection(selection_id=8, sequence=601)
    )
    response = parse_list_selections_changed_since_response(_changed(rows))
    assert [row.selection_sequence_number for row in response.selections] == [600, 601]


def test_non_success_return_status_is_typed_before_row_publication() -> None:
    status = (
        '<ReturnStatus Code="533" '
        'Description="PunterNotAuthorisedForAPI" CallId="seq-err" />'
    )
    with pytest.raises(BetdaqProviderStatusError) as exc:
        parse_list_selections_changed_since_response(
            _changed(_selection(), status=status)
        )

    assert exc.value.code == 533
    assert exc.value.call_id == "seq-err"


def test_nonfinite_economics_and_malformed_timestamp_fail_closed() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="finite"):
        parse_list_selections_changed_since_response(
            _changed(_selection().replace('WithdrawalFactor="0.125"', 'WithdrawalFactor="NaN"'))
        )

    for invalid in ("not-a-time", "2026-09-23"):
        with pytest.raises(BetdaqSoapProtocolError, match="ISO-8601"):
            parse_list_selections_changed_since_response(
                _changed(_selection(cancel_time=invalid))
            )


def test_unknown_numeric_status_is_preserved_as_provider_evidence() -> None:
    payload = _changed(_selection().replace('Status="2"', 'Status="32767"'))
    response = parse_list_selections_changed_since_response(payload)
    assert response.selections[0].status_code == 32767


def test_wrong_namespace_unexpected_child_and_dtd_fail_closed() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="Response"):
        parse_list_selections_changed_since_response(
            _changed(_selection()).replace(API, API.lower())
        )

    with pytest.raises(BetdaqSoapProtocolError, match="unexpected child"):
        parse_list_selections_changed_since_response(
            _changed(_selection(), extra="<Unexpected />")
        )

    with pytest.raises(BetdaqSoapProtocolError, match="DTD/entity"):
        parse_list_selections_changed_since_response(
            '<!DOCTYPE x [<!ENTITY boom "boom">]>' + _changed(_selection())
        )


def test_soap_fault_is_never_empty_changed_truth() -> None:
    fault = f"""<soap:Envelope xmlns:soap="{SOAP11}">
      <soap:Body>
        <soap:Fault>
          <faultcode>soap:Server</faultcode>
          <faultstring>provider unavailable</faultstring>
        </soap:Fault>
      </soap:Body>
    </soap:Envelope>"""
    with pytest.raises(BetdaqSoapFaultError):
        parse_list_selections_changed_since_response(fault)

def test_unknown_wire_attributes_fail_closed_instead_of_aliasing_modeled_evidence() -> None:
    current_extra = _current().replace(
        'SelectionSequenceNumber="1234"',
        'SelectionSequenceNumber="1234" FutureSemanticField="future"',
    )
    with pytest.raises(BetdaqSoapProtocolError, match="unexpected attribute"):
        parse_get_current_selection_sequence_number_response(current_extra)

    changed_result_extra = _changed(_selection()).replace(
        "<ListSelectionsChangedSinceResult>",
        '<ListSelectionsChangedSinceResult FutureSemanticField="future">',
    )
    with pytest.raises(BetdaqSoapProtocolError, match="unexpected attribute"):
        parse_list_selections_changed_since_response(changed_result_extra)

    selection_extra = _changed(_selection()).replace(
        'CancelOrdersTime="2026-09-23T01:10:00Z"',
        'CancelOrdersTime="2026-09-23T01:10:00Z" FutureSemanticField="future"',
    )
    with pytest.raises(BetdaqSoapProtocolError, match="unexpected attribute"):
        parse_list_selections_changed_since_response(selection_extra)

    settlement = """
      <SettlementInformation
        SettledTime="2026-09-23T01:12:00+00:00"
        VoidPercentage="0"
        LeftSideFactor="1.0"
        RightSideFactor="0.5"
        SettlementResultString="Win"
        FutureSemanticField="future" />
    """
    with pytest.raises(BetdaqSoapProtocolError, match="unexpected attribute"):
        parse_list_selections_changed_since_response(
            _changed(_selection(settlement=settlement))
        )

    return_status_extra = _changed(
        _selection(),
        status=(
            '<ReturnStatus Code="0" Description="Success" '
            'CallId="seq-1" FutureSemanticField="future" />'
        ),
    )
    with pytest.raises(BetdaqSoapProtocolError, match="unexpected attribute"):
        parse_list_selections_changed_since_response(return_status_extra)

def test_current_sequence_enforces_xsd_long_lexical_and_width() -> None:
    max_long = 2**63 - 1
    response = parse_get_current_selection_sequence_number_response(
        _current(max_long)
    )
    assert response.selection_sequence_number == max_long

    for invalid in (str(2**63), "1_234"):
        payload = _current().replace(
            'SelectionSequenceNumber="1234"',
            f'SelectionSequenceNumber="{invalid}"',
        )
        with pytest.raises(BetdaqSoapProtocolError, match="XML Schema"):
            parse_get_current_selection_sequence_number_response(payload)


@pytest.mark.parametrize(
    "row",
    (
        _selection(selection_id=2**63),
        _selection(market_id=2**63),
        _selection(sequence=2**63),
        _selection().replace('DisplayOrder="3"', f'DisplayOrder="{2**31}"'),
        _selection().replace('DisplayOrder="3"', f'DisplayOrder="{-2**31 - 1}"'),
        _selection().replace('Status="2"', 'Status="32768"'),
        _selection().replace('ResetCount="4"', 'ResetCount="32768"'),
        _selection().replace('Status="2"', 'Status="2_0"'),
    ),
)
def test_changed_selection_rejects_out_of_xsd_integer_domain(row: str) -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="XML Schema"):
        parse_list_selections_changed_since_response(_changed(row))


def test_changed_selection_accepts_exact_xsd_integer_boundaries() -> None:
    max_long = 2**63 - 1
    row = _selection(
        selection_id=max_long,
        market_id=max_long,
        sequence=max_long,
    )
    row = row.replace('DisplayOrder="3"', f'DisplayOrder="{2**31 - 1}"')
    row = row.replace('Status="2"', 'Status="32767"')
    row = row.replace('ResetCount="4"', 'ResetCount="32767"')

    response = parse_list_selections_changed_since_response(_changed(row))
    item = response.selections[0]
    assert item.selection_id == max_long
    assert item.market_id == max_long
    assert item.selection_sequence_number == max_long
    assert item.display_order == 2**31 - 1
    assert item.status_code == 32767
    assert item.reset_count == 32767

