from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from autosport.betdaq_account_readonly import BetdaqCredentials
from autosport.betdaq_event_tree_request_wire import (
    BETDAQ_EVENT_SUBTREE_ENDPOINT,
    BETDAQ_EVENT_SUBTREE_SOAP_ACTION,
    BetdaqEventSubTreeRequest,
    build_get_event_subtree_no_selections_soap11_request,
)
from autosport.betdaq_readonly_market_wire import EXTERNAL_API_NS, SOAP11_NS


def _credentials() -> BetdaqCredentials:
    return BetdaqCredentials(
        username="fixture-user",
        password="fixture-password",
        application_identifier="fixture-app",
        version="2.0",
        language_code="en",
    )


def test_event_subtree_request_serializes_exact_readonly_contract_without_secrets() -> None:
    wire = build_get_event_subtree_no_selections_soap11_request(
        _credentials(),
        BetdaqEventSubTreeRequest(
            (100, 200),
            want_direct_descendents_only=False,
            want_play_markets=True,
        ),
    )

    assert wire.endpoint == BETDAQ_EVENT_SUBTREE_ENDPOINT
    assert wire.headers == {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": f'"{BETDAQ_EVENT_SUBTREE_SOAP_ACTION}"',
    }
    assert b"fixture-password" not in wire.body
    assert b"fixture-app" not in wire.body

    root = ET.fromstring(wire.body)
    header = root.find(
        f"{{{SOAP11_NS}}}Header/{{{EXTERNAL_API_NS}}}ExternalApiHeader"
    )
    assert header is not None
    assert header.attrib == {
        "version": "2.0",
        "languageCode": "en",
        "username": "fixture-user",
        "password": "",
        "applicationIdentifier": "",
    }
    request = root.find(
        f"{{{SOAP11_NS}}}Body/"
        f"{{{EXTERNAL_API_NS}}}GetEventSubTreeNoSelections/"
        f"{{{EXTERNAL_API_NS}}}getEventSubTreeNoSelectionsRequest"
    )
    assert request is not None
    assert request.attrib == {
        "WantDirectDescendentsOnly": "false",
        "WantPlayMarkets": "true",
    }
    ids = request.findall(f"{{{EXTERNAL_API_NS}}}EventClassifierIds")
    assert [item.text for item in ids] == ["100", "200"]


@pytest.mark.parametrize(
    "ids",
    [
        (),
        (100, 100),
        (-1,),
        (True,),
        ("100",),
        ((1 << 63),),
    ],
)
def test_event_subtree_request_rejects_noncanonical_scope(ids) -> None:
    with pytest.raises((TypeError, ValueError)):
        BetdaqEventSubTreeRequest(ids)


def test_event_subtree_wire_repr_redacts_body() -> None:
    wire = build_get_event_subtree_no_selections_soap11_request(
        _credentials(),
        BetdaqEventSubTreeRequest((100,)),
    )
    text = repr(wire)
    assert "fixture-user" not in text
    assert "fixture-password" not in text
    assert "fixture-app" not in text
    assert "<redacted " in text
