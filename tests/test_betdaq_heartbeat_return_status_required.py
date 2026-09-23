from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

import autosport.betdaq_heartbeat_safety as heartbeat_module
from autosport.betdaq_heartbeat_safety import BetdaqHeartbeatSafetyError


EXTERNAL = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


def _soap_without_return_status(method: str) -> bytes:
    envelope = ET.Element(f"{{{SOAP}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP}}}Body")
    response = ET.SubElement(body, f"{{{EXTERNAL}}}{method}Response")
    result = ET.SubElement(response, f"{{{EXTERNAL}}}{method}Result")
    if method == "Pulse":
        result.set("PerformedAt", "2026-09-23T00:00:02Z")
        result.set("HeartbeatAction", "0")
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


@pytest.mark.parametrize(
    "method",
    [
        "RegisterHeartbeat",
        "ChangeHeartbeatRegistration",
        "DeregisterHeartbeat",
        "Pulse",
    ],
)
def test_heartbeat_success_requires_explicit_provider_return_status(
    method: str,
) -> None:
    payload = _soap_without_return_status(method)

    with pytest.raises(BetdaqHeartbeatSafetyError, match="ReturnStatus"):
        heartbeat_module._parse_provider_result(
            payload,
            method,
            "2026-09-23T00:00:03Z",
        )
