from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
import xml.etree.ElementTree as ET

import pytest

import autosport.betdaq_account_readonly as account_readonly
import autosport.betdaq_odds_ladder_acquisition as ladder_acq
from autosport.betdaq_account_readonly import BetdaqCredentials
from autosport.betdaq_odds_ladder_acquisition import (
    BETDAQ_DECIMAL_PRICE_FORMAT,
    BETDAQ_ODDS_LADDER_ENDPOINT,
    BETDAQ_ODDS_LADDER_SOAP_ACTION,
    BetdaqOddsLadderAcquirer,
    BetdaqOddsLadderAcquisitionError,
    BetdaqOddsLadderRequest,
    BetdaqOddsLadderUseError,
    qualify_exact_price_for_local_use,
    restore_structural_odds_ladder_observation,
)


API = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
WSU = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd"
)


def _credentials(
    *,
    username: str = "test-user",
    password: str = "test-password",
    application_identifier: str = "test-application",
) -> BetdaqCredentials:
    return BetdaqCredentials(
        username=username,
        password=password,
        application_identifier=application_identifier,
    )


def _response(
    *,
    entries: str = (
        '<Ladder price="1.50" representation="1/2" />'
        '<Ladder price="2.00" representation="Evens" />'
        '<Ladder price="3.25" representation="9/4" />'
    ),
    created: str | None = None,
    return_status: str = "",
) -> bytes:
    header = ""
    if created is not None:
        header = f"""<soap:Header xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}">
          <wsse:Security>
            <wsu:Timestamp><wsu:Created>{created}</wsu:Created></wsu:Timestamp>
          </wsse:Security>
        </soap:Header>"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <soap:Envelope xmlns:soap="{SOAP}">
      {header}
      <soap:Body>
        <GetOddsLadderResponse xmlns="{API}">
          <GetOddsLadderResult>
            {return_status}
            {entries}
          </GetOddsLadderResult>
        </GetOddsLadderResponse>
      </soap:Body>
    </soap:Envelope>""".encode("utf-8")


class RecordingTransport:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, str], bytes, float]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append((url, dict(headers), body, timeout_seconds))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


class _HttpResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> "_HttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def _canonical_observation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: bytes | None = None,
):
    response = _response() if payload is None else payload
    calls = []

    def fake_urlopen(request, *, timeout):
        calls.append((request, timeout))
        return _HttpResponse(response)

    monkeypatch.setattr(account_readonly, "urlopen", fake_urlopen)
    acquirer = BetdaqOddsLadderAcquirer(
        credentials=_credentials(),
        acquisition_id_factory=lambda: "a" * 32,
    )
    observation = acquirer.acquire()
    return observation, calls


def _white_box_positive_observation(monkeypatch: pytest.MonkeyPatch):
    """Exercise downstream use policy without treating fake network as live origin."""
    observation, calls = _canonical_observation(monkeypatch)
    return (
        replace(
            observation,
            _origin_witness=ladder_acq._ORIGIN_WITNESS,
            _clock_witness=ladder_acq._CLOCK_WITNESS,
        ),
        calls,
    )


def test_request_is_exact_readonly_decimal_ladder_call() -> None:
    transport = RecordingTransport([_response()])
    acquirer = BetdaqOddsLadderAcquirer(
        credentials=_credentials(),
        transport=transport,
        timeout_seconds=7.5,
        clock=lambda: "2026-09-23T00:00:01Z",
        acquisition_id_factory=lambda: "1" * 32,
    )

    observation = acquirer.acquire()
    assert len(transport.calls) == 1
    url, headers, body, timeout = transport.calls[0]
    assert url == BETDAQ_ODDS_LADDER_ENDPOINT
    assert timeout == 7.5
    assert headers == {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": f'"{BETDAQ_ODDS_LADDER_SOAP_ACTION}"',
    }

    root = ET.fromstring(body)
    request_nodes = root.findall(
        f".//{{{API}}}GetOddsLadder/{{{API}}}getOddsLadderRequest"
    )
    assert len(request_nodes) == 1
    assert request_nodes[0].attrib == {"PriceFormat": str(BETDAQ_DECIMAL_PRICE_FORMAT)}
    text = body.decode("utf-8")
    assert "PlaceOrders" not in text
    assert "UpdateOrders" not in text
    assert "CancelOrders" not in text
    assert observation.price_format == 1
    assert observation.exact_entry(Decimal("2.0")).price == Decimal("2.00")


def test_only_decimal_price_format_is_supported_and_failure_is_before_io() -> None:
    transport = RecordingTransport([_response()])
    with pytest.raises(ValueError, match="PriceFormat=1"):
        BetdaqOddsLadderRequest(price_format=2)
    assert transport.calls == []


def test_injected_transport_never_mints_provider_origin_authority() -> None:
    transport = RecordingTransport([_response()])
    acquirer = BetdaqOddsLadderAcquirer(
        credentials=_credentials(),
        transport=transport,
        acquisition_id_factory=lambda: "2" * 32,
    )
    observation = acquirer.acquire()

    assert observation.provider_origin_verified is False
    assert observation.receipt_clock_verified is True
    assert observation.live_entitlement_verified is False
    assert observation.provider_freshness_proven is False
    assert observation.write_permission_proven is False
    with pytest.raises(BetdaqOddsLadderUseError, match="provider origin"):
        qualify_exact_price_for_local_use(
            observation,
            Decimal("2.00"),
            max_age_seconds=30,
        )


def test_same_provider_bytes_do_not_collapse_distinct_acquisitions() -> None:
    payload = _response()
    ids = iter(("3" * 32, "4" * 32))
    transport = RecordingTransport([payload, payload])
    acquirer = BetdaqOddsLadderAcquirer(
        credentials=_credentials(),
        transport=transport,
        clock=lambda: "2026-09-23T00:00:01Z",
        acquisition_id_factory=lambda: next(ids),
    )

    first = acquirer.acquire()
    second = acquirer.acquire()

    assert first.response_sha256 == second.response_sha256
    assert first.content_sha256 == second.content_sha256
    assert first.request_fingerprint_sha256 == second.request_fingerprint_sha256
    assert first.acquisition_id != second.acquisition_id
    assert first.evidence_sha256 != second.evidence_sha256


def test_request_fingerprint_and_record_do_not_persist_credentials() -> None:
    payload = _response()
    first = BetdaqOddsLadderAcquirer(
        credentials=_credentials(
            username="user-one",
            password="very-secret-one",
            application_identifier="app-secret-one",
        ),
        transport=RecordingTransport([payload]),
        clock=lambda: "2026-09-23T00:00:01Z",
        acquisition_id_factory=lambda: "5" * 32,
    ).acquire()
    second = BetdaqOddsLadderAcquirer(
        credentials=_credentials(
            username="user-two",
            password="very-secret-two",
            application_identifier="app-secret-two",
        ),
        transport=RecordingTransport([payload]),
        clock=lambda: "2026-09-23T00:00:01Z",
        acquisition_id_factory=lambda: "6" * 32,
    ).acquire()

    assert first.request_fingerprint_sha256 == second.request_fingerprint_sha256
    serialized = json.dumps(first.to_safe_record(), sort_keys=True)
    for secret in ("user-one", "very-secret-one", "app-secret-one"):
        assert secret not in serialized


def test_transport_failure_is_redacted_even_for_custom_transport() -> None:
    secret = "password=do-not-leak"
    transport = RecordingTransport([RuntimeError(secret)])
    acquirer = BetdaqOddsLadderAcquirer(
        credentials=_credentials(),
        transport=transport,
        acquisition_id_factory=lambda: "7" * 32,
    )

    with pytest.raises(BetdaqOddsLadderAcquisitionError) as exc:
        acquirer.acquire()
    assert "transport failed" in str(exc.value)
    assert secret not in str(exc.value)


def test_non_bytes_transport_payload_is_rejected() -> None:
    transport = RecordingTransport(["not-bytes"])
    acquirer = BetdaqOddsLadderAcquirer(
        credentials=_credentials(),
        transport=transport,
        acquisition_id_factory=lambda: "8" * 32,
    )
    with pytest.raises(BetdaqOddsLadderAcquisitionError, match="non-bytes"):
        acquirer.acquire()


def test_provider_message_time_is_preserved_but_never_relabelled_freshness() -> None:
    observation = BetdaqOddsLadderAcquirer(
        credentials=_credentials(),
        transport=RecordingTransport(
            [_response(created="2026-09-23T00:00:00.250Z")]
        ),
        clock=lambda: "2026-09-23T00:00:01Z",
        acquisition_id_factory=lambda: "9" * 32,
    ).acquire()

    assert observation.provider_message_created_at == "2026-09-23T00:00:00.250Z"
    assert observation.provider_freshness_proven is False


def test_monkeypatched_urlopen_cannot_mint_provider_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, calls = _canonical_observation(monkeypatch)

    assert observation.provider_origin_verified is False
    assert observation.receipt_clock_verified is True
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == BETDAQ_ODDS_LADDER_ENDPOINT
    assert timeout == 10.0
    with pytest.raises(BetdaqOddsLadderUseError, match="provider origin"):
        qualify_exact_price_for_local_use(
            observation,
            Decimal("2.00"),
            max_age_seconds=60,
        )


def test_narrow_use_evidence_never_claims_provider_freshness_or_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, _ = _white_box_positive_observation(monkeypatch)
    use = qualify_exact_price_for_local_use(
        observation,
        Decimal("2.00"),
        max_age_seconds=60,
    )
    assert use.price == Decimal("2.00")
    assert use.provider_origin_verified is True
    assert use.receipt_clock_verified is True
    assert use.provider_freshness_proven is False
    assert use.write_permission_proven is False

def test_off_ladder_price_is_rejected_without_rounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, _ = _white_box_positive_observation(monkeypatch)
    with pytest.raises(BetdaqOddsLadderUseError, match="not present"):
        qualify_exact_price_for_local_use(
            observation,
            Decimal("2.01"),
            max_age_seconds=60,
        )


def _retime_positive_observation(observation, observed_at: str):
    digest = ladder_acq._evidence_sha(
        observation.acquisition_id,
        observed_at,
        observation.price_format,
        observation.request_fingerprint_sha256,
        observation.response_sha256,
        observation.response_byte_count,
        observation.content_sha256,
        observation.provider_message_created_at,
        observation.call_id,
    )
    return replace(
        observation,
        observed_at=observed_at,
        evidence_sha256=digest,
    )


def test_stale_and_clock_rollback_observations_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, _ = _white_box_positive_observation(monkeypatch)

    stale = _retime_positive_observation(observation, "2020-01-01T00:00:00Z")
    with pytest.raises(BetdaqOddsLadderUseError, match="use horizon"):
        qualify_exact_price_for_local_use(
            stale,
            Decimal("2.00"),
            max_age_seconds=1,
        )

    future = _retime_positive_observation(observation, "2100-01-01T00:00:00Z")
    with pytest.raises(BetdaqOddsLadderUseError, match="moved behind"):
        qualify_exact_price_for_local_use(
            future,
            Decimal("2.00"),
            max_age_seconds=2_147_483_647,
        )


def test_restart_restore_preserves_history_but_not_live_use_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, _ = _white_box_positive_observation(monkeypatch)
    record = observation.to_safe_record()
    assert record["historical_provider_origin_verified"] is True
    assert record["historical_receipt_clock_verified"] is True

    restored = restore_structural_odds_ladder_observation(record)
    assert restored.evidence_sha256 == observation.evidence_sha256
    assert restored.entries == observation.entries
    assert restored.provider_origin_verified is False
    assert restored.receipt_clock_verified is False
    with pytest.raises(BetdaqOddsLadderUseError, match="provider origin"):
        qualify_exact_price_for_local_use(
            restored,
            Decimal("2.00"),
            max_age_seconds=60,
        )


def test_restart_record_entry_tamper_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, _ = _canonical_observation(monkeypatch)
    record = observation.to_safe_record()
    entries = [dict(item) for item in record["entries"]]
    entries[1]["price_text"] = "2.02"
    record["entries"] = entries

    with pytest.raises(BetdaqOddsLadderAcquisitionError, match="content_sha256"):
        restore_structural_odds_ladder_observation(record)


def test_custom_clock_cannot_mint_positive_local_use_even_with_valid_bytes() -> None:
    observation = BetdaqOddsLadderAcquirer(
        credentials=_credentials(),
        transport=RecordingTransport([_response()]),
        clock=lambda: "2026-09-23T00:00:01Z",
        acquisition_id_factory=lambda: "b" * 32,
    ).acquire()
    assert observation.receipt_clock_verified is False
    with pytest.raises(BetdaqOddsLadderUseError):
        qualify_exact_price_for_local_use(
            observation,
            Decimal("2.00"),
            max_age_seconds=60,
        )
