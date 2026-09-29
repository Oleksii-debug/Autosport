from __future__ import annotations

from datetime import datetime, timezone
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
from autosport.betdaq_rate_governor import (
    BetdaqRateDeferred,
    default_betdaq_rate_policy,
    resolve_betdaq_rate_governor,
)
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


class _RateClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _rate_governor(tmp_path, *, release_cold_start: bool = True):
    rate_clock = _RateClock()
    governor = resolve_betdaq_rate_governor(
        tmp_path,
        default_betdaq_rate_policy(),
        clock=rate_clock,
        wall_clock=lambda: datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    if release_cold_start:
        rate_clock.value = 61.0
    return governor, rate_clock


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


def _provider(transport, *, max_attempts: int, rate_governor) -> BetdaqLiveReadOnlyProvider:
    return BetdaqLiveReadOnlyProvider(
        credentials=_credentials(),
        rate_governor=rate_governor,
        transport=transport,
        market_bindings=[BetdaqMarketBinding(9001, "event-1", "football")],
        threshold_amount=Decimal("1.50"),
        max_attempts=max_attempts,
        clock=lambda: "2026-09-23T19:00:00Z",
    )


def test_bridge_delegates_exactly_one_getprices_post_without_own_retry(tmp_path) -> None:
    post = _PostTransport()
    governor, _ = _rate_governor(tmp_path)
    bridge = BetdaqReadOnlyLiveTransport(
        credentials=_credentials(),
        rate_governor=governor,
        transport=post,
    )

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
    admission = bridge.last_rate_admission
    assert admission is not None
    assert admission.method == "GetPrices"
    assert admission.sequence == 1
    assert admission.grants_execution_authority is False
    assert admission.grants_write_permission is False


def test_injected_post_transport_is_not_the_canonical_product_transport(tmp_path) -> None:
    governor, _ = _rate_governor(tmp_path)
    bridge = BetdaqReadOnlyLiveTransport(
        credentials=_credentials(),
        rate_governor=governor,
        transport=_PostTransport(),
    )
    assert bridge.canonical_transport_selected is False


def test_default_bridge_reuses_product_owned_transport_without_claiming_origin(tmp_path) -> None:
    governor, _ = _rate_governor(tmp_path)
    bridge = BetdaqReadOnlyLiveTransport(
        credentials=_credentials(),
        rate_governor=governor,
    )
    assert bridge.canonical_transport_selected is True
    text = repr(bridge)
    assert "fixture-user" not in text
    assert "fixture-password" not in text
    assert "fixture-app" not in text
    assert "provider_origin_verified" not in text


def test_arbitrary_transport_exception_is_redacted_and_not_promoted_to_transient(tmp_path) -> None:
    post = _ExplodingPostTransport()
    governor, _ = _rate_governor(tmp_path)
    bridge = BetdaqReadOnlyLiveTransport(
        credentials=_credentials(),
        rate_governor=governor,
        transport=post,
    )
    with pytest.raises(
        ProviderUnavailableError,
        match=r"^BETDAQ GetPrices transport failed without retryable classification$",
    ) as raised:
        bridge.get_prices(_request(), timeout_seconds=1.0)
    assert post.calls == 1
    assert "fixture-password" not in str(raised.value)


def test_provider_does_not_retry_unclassified_custom_failure(tmp_path) -> None:
    post = _ExplodingPostTransport()
    governor, _ = _rate_governor(tmp_path)
    provider = _provider(post, max_attempts=5, rate_governor=governor)
    with pytest.raises(
        ProviderUnavailableError,
        match="without retryable classification",
    ):
        provider.read_batch()
    assert post.calls == 1
    assert provider.last_request_evidence is None


def test_provider_does_not_retry_opaque_canonical_transport_error(tmp_path) -> None:
    post = _OpaqueCanonicalErrorTransport()
    governor, _ = _rate_governor(tmp_path)
    provider = _provider(post, max_attempts=5, rate_governor=governor)
    with pytest.raises(
        ProviderUnavailableError,
        match="without retryable classification",
    ) as raised:
        provider.read_batch()
    assert post.calls == 1
    assert "fixture-password" not in str(raised.value)
    assert provider.last_request_evidence is None


def test_provider_retries_only_preserved_explicit_transient_signal(tmp_path) -> None:
    post = _ExplicitTransientPostTransport()
    governor, _ = _rate_governor(tmp_path)
    provider = _provider(post, max_attempts=2, rate_governor=governor)
    with pytest.raises(ProviderUnavailableError, match="after 2 bounded attempts"):
        provider.read_batch()
    assert post.calls == 2
    admission = provider.live_transport.last_rate_admission
    assert admission is not None
    assert admission.sequence == 2
    assert provider.last_request_evidence is None


def test_rate_cold_start_denial_occurs_before_http_dispatch(tmp_path) -> None:
    post = _PostTransport()
    governor, _ = _rate_governor(tmp_path, release_cold_start=False)
    bridge = BetdaqReadOnlyLiveTransport(
        credentials=_credentials(),
        rate_governor=governor,
        transport=post,
    )

    with pytest.raises(BetdaqRateDeferred, match="cold_start_rate_window_unproven"):
        bridge.get_prices(_request(), timeout_seconds=1.0)

    assert post.calls == []
    assert bridge.last_rate_admission is None


def test_live_transport_rejects_noncanonical_rate_governor_before_dispatch() -> None:
    with pytest.raises(TypeError, match="canonical BetdaqRateGovernor"):
        BetdaqReadOnlyLiveTransport(
            credentials=_credentials(),
            rate_governor=object(),
            transport=_PostTransport(),
        )


def test_non_bytes_transport_contract_failure_is_not_retried(tmp_path) -> None:
    post = _PostTransport(payload="not-bytes")
    governor, _ = _rate_governor(tmp_path)
    provider = _provider(post, max_attempts=4, rate_governor=governor)
    with pytest.raises(ProviderUnavailableError, match="non-bytes payload"):
        provider.read_batch()
    assert len(post.calls) == 1
    assert provider.last_request_evidence is None


def test_live_provider_keeps_origin_unverified_after_valid_snapshot(tmp_path) -> None:
    post = _PostTransport()
    governor, _ = _rate_governor(tmp_path)
    provider = _provider(post, max_attempts=1, rate_governor=governor)

    batch = provider.read_batch()

    assert len(batch.quotes) == 2
    assert "UNVERIFIED_PROVIDER_ORIGIN" in batch.quality_flags
    assert "LIVE_ENTITLEMENT_UNVERIFIED" in batch.quality_flags
    evidence = provider.last_request_evidence
    assert evidence is not None
    assert evidence.provider_origin_verified is False
    assert evidence.live_entitlement_verified is False


def test_default_live_provider_composes_canonical_transport_without_origin_promotion(tmp_path) -> None:
    governor, _ = _rate_governor(tmp_path)
    provider = BetdaqLiveReadOnlyProvider(
        credentials=_credentials(),
        rate_governor=governor,
        market_bindings=[BetdaqMarketBinding(9001, "event-1", "football")],
        threshold_amount=Decimal("1.50"),
    )
    assert provider.live_transport.canonical_transport_selected is True
    assert provider.last_request_evidence is None
