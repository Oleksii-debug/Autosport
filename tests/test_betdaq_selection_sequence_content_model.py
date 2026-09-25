from __future__ import annotations

import pytest

from autosport.betdaq_readonly_market_wire import BetdaqSoapProtocolError
from autosport.betdaq_selection_sequence_wire import (
    parse_get_current_selection_sequence_number_response,
    parse_list_selections_changed_since_response,
)


API = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"


def _envelope(operation: str, result: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="{SOAP11}">
  <soap:Body>
    <{operation}Response xmlns="{API}">
      {result}
    </{operation}Response>
  </soap:Body>
</soap:Envelope>"""


def _current(inner: str = "") -> str:
    return _envelope(
        "GetCurrentSelectionSequenceNumber",
        (
            '<GetCurrentSelectionSequenceNumberResult SelectionSequenceNumber="1234">'
            f"{inner}"
            "</GetCurrentSelectionSequenceNumberResult>"
        ),
    )


def _settlement(*, tail: str = "") -> str:
    return (
        '<SettlementInformation SettledTime="2026-09-23T01:12:00Z" '
        'VoidPercentage="0" LeftSideFactor="1.0" RightSideFactor="0.5" '
        'SettlementResultString="Win" />'
        f"{tail}"
    )


def _selection(inner: str = "") -> str:
    return f"""<Selections
      Id="77"
      Name="Home"
      DisplayOrder="3"
      IsHidden="false"
      Status="2"
      ResetCount="4"
      WithdrawalFactor="0.125"
      MarketId="9001"
      SelectionSequenceNumber="1235"
      CancelOrdersTime="2026-09-23T01:10:00Z">
      {inner}
    </Selections>"""


def _changed(inner: str) -> str:
    return _envelope(
        "ListSelectionsChangedSince",
        f"""<ListSelectionsChangedSinceResult>
          {inner}
        </ListSelectionsChangedSinceResult>""",
    )


def test_current_result_rejects_non_whitespace_character_content() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="character content"):
        parse_get_current_selection_sequence_number_response(_current("FORGED"))


def test_changed_result_rejects_non_whitespace_character_content() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="character content"):
        parse_list_selections_changed_since_response(
            _changed("FORGED" + _selection())
        )


def test_selection_rejects_non_whitespace_character_content() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="character content"):
        parse_list_selections_changed_since_response(
            _changed(_selection("FORGED"))
        )


def test_selection_rejects_non_whitespace_child_tail() -> None:
    with pytest.raises(BetdaqSoapProtocolError, match="character content"):
        parse_list_selections_changed_since_response(
            _changed(_selection(_settlement(tail="FORGED")))
        )


def test_return_status_rejects_unmodeled_child_content() -> None:
    status = (
        '<ReturnStatus Code="0" Description="Success" CallId="seq-1">'
        '<FutureAuthority />'
        "</ReturnStatus>"
    )
    with pytest.raises(BetdaqSoapProtocolError, match="child content"):
        parse_get_current_selection_sequence_number_response(_current(status))


def test_return_status_rejects_non_whitespace_character_content() -> None:
    status = (
        '<ReturnStatus Code="0" Description="Success" CallId="seq-1">'
        "FORGED"
        "</ReturnStatus>"
    )
    with pytest.raises(BetdaqSoapProtocolError, match="character content"):
        parse_get_current_selection_sequence_number_response(_current(status))


def test_formatting_whitespace_remains_legal() -> None:
    status = '<ReturnStatus Code="0" Description="Success" CallId="seq-1" />'
    current = parse_get_current_selection_sequence_number_response(
        _current("\n  " + status + "\n")
    )
    assert current.return_status_present is True
    assert current.selection_sequence_number == 1234

    changed = parse_list_selections_changed_since_response(
        _changed("\n  " + _selection("\n    " + _settlement() + "\n  ") + "\n")
    )
    assert len(changed.selections) == 1
    assert len(changed.selections[0].settlement_information) == 1
