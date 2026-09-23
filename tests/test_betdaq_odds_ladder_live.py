from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betdaq_account_readonly import (
    BetdaqCredentials,
    UrllibBetdaqSoapTransport,
)
from autosport.betdaq_odds_ladder_live import (
    BetdaqOddsLadderAcquisitionError,
    BetdaqPriceFormat,
    _bind_observation,
    _canonical_transport,
    acquire_odds_ladder,
)
from autosport.betdaq_odds_ladder_wire import parse_get_odds_ladder_response


API = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


def _response(entries: str | None = None) -> bytes:
    ladder = entries or (
        '<Ladder price="1.50" representation="1/2" />'
        '<Ladder price="2.00" representation="Evens" />'
        '<Ladder price="3.25" representation="9/4" />'
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="{SOAP}">
  <soap:Body>
    <GetOddsLadderResponse xmlns="{API}">
      <GetOddsLadderResult>{ladder}</GetOddsLadderResult>
    </GetOddsLadderResponse>
  </soap:Body>
</soap:Envelope>""".encode()


def _credentials() -> BetdaqCredentials:
    return BetdaqCredentials(
        username="fixture-user",
        password="fixture-secret",
        application_identifier="fixture-app",
    )


class FakeTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str], bytes, float]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append((url, headers, body, timeout_seconds))
        return self.payload


def test_injected_transport_can_parse_but_cannot_mint_live_price_authority() -> None:
    transport = FakeTransport(_response())
    observation = acquire_odds_ladder(_credentials(), transport=transport)

    assert observation.transport_origin == "INJECTED_TEST"
    assert observation.live_provider_origin_verified is False
    assert observation.entries == (
        Decimal("1.50"),
        Decimal("2.00"),
        Decimal("3.25"),
    )
    assert observation.admits_price(
        Decimal("2.00"), max_age_seconds=Decimal("30")
    ) is False
    assert observation.grants_write_permission is False


def test_request_binds_exact_price_format_without_persisting_credentials_in_identity() -> None:
    decimal_transport = FakeTransport(_response())
    fractional_transport = FakeTransport(_response())

    decimal = acquire_odds_ladder(
        _credentials(),
        price_format=BetdaqPriceFormat.DECIMAL,
        transport=decimal_transport,
    )
    fractional = acquire_odds_ladder(
        _credentials(),
        price_format=BetdaqPriceFormat.FRACTIONAL,
        transport=fractional_transport,
    )

    assert decimal.request_identity_sha256 != fractional.request_identity_sha256
    assert b">1</" in decimal_transport.calls[0][2]
    assert b">2</" in fractional_transport.calls[0][2]
    assert "fixture-user" not in decimal.request_identity_sha256
    assert "fixture-secret" not in decimal.request_identity_sha256


def test_same_bytes_acquired_twice_are_distinct_observation_generations() -> None:
    payload = _response()
    first = acquire_odds_ladder(_credentials(), transport=FakeTransport(payload))
    second = acquire_odds_ladder(_credentials(), transport=FakeTransport(payload))

    assert first.response_sha256 == second.response_sha256
    assert first.content_sha256 == second.content_sha256
    assert first.acquisition_generation != second.acquisition_generation
    assert first.observation_id != second.observation_id


def test_exact_live_membership_is_freshness_bounded_and_never_rounded() -> None:
    payload = _response()
    parsed = parse_get_odds_ladder_response(payload)
    observation = _bind_observation(
        parsed,
        price_format=BetdaqPriceFormat.DECIMAL,
        raw_response=payload,
        canonical_origin=True,
    )

    assert observation.live_provider_origin_verified is True
    assert observation.admits_price(
        Decimal("2.00"),
        max_age_seconds=Decimal("10"),
        now_monotonic_ns=observation.received_monotonic_ns + 9_000_000_000,
    ) is True
    assert observation.admits_price(
        Decimal("2.01"),
        max_age_seconds=Decimal("10"),
        now_monotonic_ns=observation.received_monotonic_ns + 1,
    ) is False
    assert observation.admits_price(
        Decimal("2.00"),
        max_age_seconds=Decimal("10"),
        now_monotonic_ns=observation.received_monotonic_ns + 11_000_000_000,
    ) is False
    assert observation.admits_price(
        Decimal("2.00"),
        max_age_seconds=Decimal("10"),
        now_monotonic_ns=observation.received_monotonic_ns - 1,
    ) is False


def test_serialized_audit_evidence_cannot_represent_durable_live_origin_authority() -> None:
    payload = _response()
    observation = _bind_observation(
        parse_get_odds_ladder_response(payload),
        price_format=BetdaqPriceFormat.DECIMAL,
        raw_response=payload,
        canonical_origin=True,
    )

    encoded = observation.to_canonical_dict()

    assert encoded["live_provider_origin_verified"] is False
    assert encoded["grants_write_permission"] is False
    assert "received_monotonic_ns" not in encoded


def test_canonical_origin_requires_exact_unmodified_product_transport() -> None:
    assert _canonical_transport(UrllibBetdaqSoapTransport()) is True
    assert _canonical_transport(FakeTransport(_response())) is False


class SecretFailingTransport:
    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        raise RuntimeError("fixture-secret must never escape")


def test_transport_failure_redacts_arbitrary_exception_text() -> None:
    with pytest.raises(BetdaqOddsLadderAcquisitionError) as caught:
        acquire_odds_ladder(_credentials(), transport=SecretFailingTransport())

    assert "fixture-secret" not in str(caught.value)
    assert str(caught.value) == "BETDAQ GetOddsLadder transport failed"


def test_parser_failure_does_not_publish_partial_observation() -> None:
    duplicate = _response(
        '<Ladder price="2.00" representation="Evens" />'
        '<Ladder price="2.0" representation="2" />'
    )
    with pytest.raises(BetdaqOddsLadderAcquisitionError) as caught:
        acquire_odds_ladder(_credentials(), transport=FakeTransport(duplicate))

    assert "validation failed" in str(caught.value)


def test_wrong_price_format_type_fails_before_network_io() -> None:
    transport = FakeTransport(_response())
    with pytest.raises(BetdaqOddsLadderAcquisitionError):
        acquire_odds_ladder(
            _credentials(),
            price_format=1,  # type: ignore[arg-type]
            transport=transport,
        )
    assert transport.calls == []
