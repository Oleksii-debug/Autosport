from __future__ import annotations

from decimal import Decimal
import xml.etree.ElementTree as ET

import pytest

from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyError,
    BetdaqCredentials,
)
from autosport.betdaq_readonly_live_provider import BetdaqLiveReadOnlyProvider
from autosport.betdaq_readonly_live_transport import BetdaqReadOnlyLiveTransport
from autosport.betdaq_readonly_market_wire import EXTERNAL_API_NS, SOAP11_NS
from autosport.betdaq_readonly_provider import (
    BETDAQ_GET_PRICES_ENDPOINT,
    BETDAQ_GET_PRICES_SOAP_ACTION,
    BetdaqGetPricesRequest,
    BetdaqMarketBinding,
)
from autosport.providers import ProviderUnavailableError


def _credentials() -> BetdaqCredentials:
    return BetdaqCredentials(
        username="fixture-user",
        password="fixture-password",
        application_identifier="fixture-app",
    )


def _response() -> bytes:
    api = EXTERNAL_API_NS
    soap = SOAP11_NS
    return f"""<soap:Envelope xmlns:soap="{soap}">
      <soap:Body>
        <GetPricesResponse xmlns="{api}">
          <GetPricesResult>
            <ReturnStatus Code="0" Description="Success" />
            <MarketPrices
              Id="9001"
              Name="Match Odds"
              Type="1"
              IsPlayMarket="false"
              Status="2"
              StartTime="2026-09-23T18:00:00Z"
              WithdrawalSequenceNumber="7"
              IsInRunningAllowed="true"
              IsManagedWhenInRunning="true"
              IsCurrentlyInRunning="false"
              InRunningDelaySeconds="5"
            >
              <Selections
                Id="501"
                Name="Player A"
                Status="3"
                ResetCount="4"
                DeductionFactor="0.125"
              >
                <ForSidePrices Price="2.00" Stake="5.00" />
                <AgainstSidePrices Price="2.10" Stake="9.50" />
              </Selections>
            </MarketPrices>
          </GetPricesResult>
        </GetPricesResponse>
      </soap:Body>
    </soap:Envelope>""".encode()


class _PostTransport:
    def __init__(self, payload: bytes | object | None = None) -> None:
        self.payload = _response() if payload is None else payload
        self.calls: list[tuple[str, dict[str, str], bytes, float]] = []

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls.append((url, dict(headers), body, timeout_seconds))
        return self.payload


class _ExplodingPostTransport:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls += 1
        raise RuntimeError("fixture-password must never escape")


class _OpaqueCanonicalErrorTransport:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls += 1
        raise BetdaqAccountReadOnlyError(
            "opaque failure containing fixture-password must never escape"
        )


class _ExplicitTransientPostTransport:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls += 1
        raise TimeoutError("explicit transient fixture")


def _request() -> BetdaqGetPricesRequest:
    return BetdaqGetPricesRequest(17, (9001,), Decimal("1.50"))


def _provider(transport, *, max_attempts: int) -> BetdaqLiveReadOnlyProvider:
    return BetdaqLiveReadOnlyProvider(
        credentials=_credentials(),
        transport=transport,
        market_bindings=[BetdaqMarketBinding(9001, "event-1", "football")],
        threshold_amount=Decimal("1.50"),
        max_attempts=max_attempts,
        clock=lambda: "2026-09-23T19:00:00Z",
    )


def test_bridge_delegates_exactly_one_getprices_post_without_own_retry() -> None:
    post = _PostTransport()
    bridge = BetdaqReadOnlyLiveTransport(credentials=_credentials(), transport=post)

    payload = bridge.get_prices(_request(), timeout_seconds=3.25)

    assert payload == _response()
    assert len(post.calls) == 1
    url, headers, body, timeout = post.calls[0]
    assert url == BETDAQ_GET_PRICES_ENDPOINT
    assert headers == {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": f'"{BETDAQ_GET_PRICES_SOAP_ACTION}"',
    }
    assert timeout == 3.25
    root = ET.fromstring(body)
    method = root.find(f"{{{SOAP11_NS}}}Body/{{{EXTERNAL_API_NS}}}GetPrices")
    assert method is not None


def test_injected_post_transport_is_not_the_canonical_product_transport() -> None:
    bridge = BetdaqReadOnlyLiveTransport(
        credentials=_credentials(),
        transport=_PostTransport(),
    )
    assert bridge.canonical_transport_selected is False


def test_default_bridge_reuses_product_owned_transport_without_claiming_origin() -> None:
    bridge = BetdaqReadOnlyLiveTransport(credentials=_credentials())
    assert bridge.canonical_transport_selected is True
    text = repr(bridge)
    assert "fixture-user" not in text
    assert "fixture-password" not in text
    assert "fixture-app" not in text
    assert "provider_origin_verified" not in text


def test_arbitrary_transport_exception_is_redacted_and_not_promoted_to_transient() -> None:
    post = _ExplodingPostTransport()
    bridge = BetdaqReadOnlyLiveTransport(credentials=_credentials(), transport=post)
    with pytest.raises(
        ProviderUnavailableError,
        match=r"^BETDAQ GetPrices transport failed without retryable classification$",
    ) as raised:
        bridge.get_prices(_request(), timeout_seconds=1.0)
    assert post.calls == 1
    assert "fixture-password" not in str(raised.value)


def test_provider_does_not_retry_unclassified_custom_failure() -> None:
    post = _ExplodingPostTransport()
    provider = _provider(post, max_attempts=5)
    with pytest.raises(
        ProviderUnavailableError,
        match="without retryable classification",
    ):
        provider.read_batch()
    assert post.calls == 1
    assert provider.last_request_evidence is None


def test_provider_does_not_retry_opaque_canonical_transport_error() -> None:
    post = _OpaqueCanonicalErrorTransport()
    provider = _provider(post, max_attempts=5)
    with pytest.raises(
        ProviderUnavailableError,
        match="without retryable classification",
    ) as raised:
        provider.read_batch()
    assert post.calls == 1
    assert "fixture-password" not in str(raised.value)
    assert provider.last_request_evidence is None


def test_provider_retries_only_preserved_explicit_transient_signal() -> None:
    post = _ExplicitTransientPostTransport()
    provider = _provider(post, max_attempts=2)
    with pytest.raises(ProviderUnavailableError, match="after 2 bounded attempts"):
        provider.read_batch()
    assert post.calls == 2
    assert provider.last_request_evidence is None


def test_non_bytes_transport_contract_failure_is_not_retried() -> None:
    post = _PostTransport(payload="not-bytes")
    provider = _provider(post, max_attempts=4)
    with pytest.raises(ProviderUnavailableError, match="non-bytes payload"):
        provider.read_batch()
    assert len(post.calls) == 1
    assert provider.last_request_evidence is None


def test_live_provider_keeps_origin_unverified_after_valid_snapshot() -> None:
    post = _PostTransport()
    provider = _provider(post, max_attempts=1)

    batch = provider.read_batch()

    assert len(batch.quotes) == 2
    assert "UNVERIFIED_PROVIDER_ORIGIN" in batch.quality_flags
    assert "LIVE_ENTITLEMENT_UNVERIFIED" in batch.quality_flags
    evidence = provider.last_request_evidence
    assert evidence is not None
    assert evidence.provider_origin_verified is False
    assert evidence.live_entitlement_verified is False


def test_default_live_provider_composes_canonical_transport_without_origin_promotion() -> None:
    provider = BetdaqLiveReadOnlyProvider(
        credentials=_credentials(),
        market_bindings=[BetdaqMarketBinding(9001, "event-1", "football")],
        threshold_amount=Decimal("1.50"),
    )
    assert provider.live_transport.canonical_transport_selected is True
    assert provider.last_request_evidence is None
