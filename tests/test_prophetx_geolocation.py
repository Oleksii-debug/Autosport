from dataclasses import replace
from decimal import Decimal
import json

import pytest

from autosport.prophetx_geolocation import (
    ProphetXGeolocationAdmission,
    ProphetXGeolocationClient,
    ProphetXGeolocationError,
    ProphetXGeolocationHttpResponse,
    ProphetXGeolocationState,
    SANDBOX_GEOLOCATION_ENDPOINT,
    UrllibProphetXGeolocationTransport,
    assert_prophetx_geolocation_admission_authoritative,
)
from autosport.real_execution_ledger import ExecutionAction

NOW = "2026-09-22T21:00:00+00:00"
EXPIRY = "2026-09-22T21:05:00+00:00"


def action(**changes):
    values = dict(
        action_id="action-prophetx-1",
        bookmaker_id="prophetx",
        account_id="acct-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="strike-1",
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("10.00"),
        quote_id="q-1",
        quote_observed_at="2026-09-22T20:59:00+00:00",
        expires_at=EXPIRY,
    )
    values.update(changes)
    return ExecutionAction(**values)


def payload(success=True, **changes):
    value = {
        "success": success,
        "country": "US",
        "state": "NJ",
        "city": "x",
        "reason": "ok",
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":")).encode()


def response(body=None, **changes):
    body = payload() if body is None else body
    values = dict(
        endpoint=SANDBOX_GEOLOCATION_ENDPOINT,
        status_code=200,
        content_type="application/json",
        content_encoding=None,
        content_length=str(len(body)),
        body=body,
    )
    values.update(changes)
    return ProphetXGeolocationHttpResponse(**values)


class FakeTransport:
    def __init__(self, result=None, error=None):
        self.result = result or response()
        self.error = error
        self.calls = []

    def post(self, endpoint, *, body, timeout_seconds):
        self.calls.append((endpoint, body, timeout_seconds))
        if self.error:
            raise self.error
        return self.result


def test_invalid_ip_never_calls_transport_and_ip_is_not_durable():
    transport = FakeTransport()
    admission = ProphetXGeolocationClient(
        transport,
        clock=lambda: NOW,
    ).check(action(), "not-an-ip")
    assert not transport.calls
    assert admission.state is ProphetXGeolocationState.UNKNOWN_LOCAL_IP
    assert "not-an-ip" not in json.dumps(admission.to_durable_dict())


def test_injected_transport_cannot_mint_provider_confirmed_allow():
    admission = ProphetXGeolocationClient(
        FakeTransport(),
        clock=lambda: NOW,
    ).check(action(), "203.0.113.9")
    assert (
        admission.state
        is ProphetXGeolocationState.UNKNOWN_UNVERIFIED_PROVIDER_ORIGIN
    )
    assert_prophetx_geolocation_admission_authoritative(admission, action())


def test_internal_fixed_origin_client_can_issue_allow(monkeypatch):
    monkeypatch.setattr(
        UrllibProphetXGeolocationTransport,
        "post",
        lambda self, endpoint, *, body, timeout_seconds: response(),
    )
    admission = ProphetXGeolocationClient(clock=lambda: NOW).check(
        action(),
        "203.0.113.9",
    )
    assert admission.state is ProphetXGeolocationState.ALLOWED_PROVIDER_CONFIRMED
    assert_prophetx_geolocation_admission_authoritative(admission, action())


def test_http_200_success_false_is_hard_provider_denial(monkeypatch):
    denied = response(payload(False, reason="not allowed"))
    monkeypatch.setattr(
        UrllibProphetXGeolocationTransport,
        "post",
        lambda self, endpoint, *, body, timeout_seconds: denied,
    )
    admission = ProphetXGeolocationClient(clock=lambda: NOW).check(
        action(),
        "203.0.113.9",
    )
    assert admission.state is ProphetXGeolocationState.DENIED_PROVIDER_CONFIRMED


def test_location_and_reason_are_not_durable(monkeypatch):
    raw = payload(
        True,
        country="SECRET_COUNTRY",
        state="SECRET_STATE",
        city="SECRET_CITY",
        reason="SECRET_REASON",
    )
    monkeypatch.setattr(
        UrllibProphetXGeolocationTransport,
        "post",
        lambda self, endpoint, *, body, timeout_seconds: response(raw),
    )
    admission = ProphetXGeolocationClient(clock=lambda: NOW).check(
        action(),
        "203.0.113.9",
    )
    durable = json.dumps(admission.to_durable_dict())
    for value in (
        "SECRET_COUNTRY",
        "SECRET_STATE",
        "SECRET_CITY",
        "SECRET_REASON",
        "203.0.113.9",
    ):
        assert value not in durable


def test_durable_decision_hash_does_not_fingerprint_location(monkeypatch):
    responses = iter(
        (
            response(
                payload(
                    True,
                    country="US",
                    state="NJ",
                    city="A",
                    reason="first",
                )
            ),
            response(
                payload(
                    True,
                    country="CA",
                    state="ON",
                    city="B",
                    reason="second",
                )
            ),
        )
    )
    monkeypatch.setattr(
        UrllibProphetXGeolocationTransport,
        "post",
        lambda self, endpoint, *, body, timeout_seconds: next(responses),
    )
    first = ProphetXGeolocationClient(clock=lambda: NOW).check(
        action(),
        "203.0.113.9",
    )
    second = ProphetXGeolocationClient(clock=lambda: NOW).check(
        action(),
        "203.0.113.9",
    )
    assert first.source_payload_sha256 == second.source_payload_sha256


@pytest.mark.parametrize(
    "bad",
    [
        b'{"success":true,"success":false,"country":"US","state":"NJ","city":"x","reason":"x"}',
        b'{"country":"US","state":"NJ","city":"x","reason":"x"}',
        b'{"success":"true","country":"US","state":"NJ","city":"x","reason":"x"}',
        b"{",
        b"[]",
        b'{"success":NaN,"country":"US","state":"NJ","city":"x","reason":"x"}',
        b"\xff",
    ],
)
def test_malformed_provider_contract_fails_closed(monkeypatch, bad):
    monkeypatch.setattr(
        UrllibProphetXGeolocationTransport,
        "post",
        lambda self, endpoint, *, body, timeout_seconds: response(bad),
    )
    admission = ProphetXGeolocationClient(clock=lambda: NOW).check(
        action(),
        "203.0.113.9",
    )
    assert admission.state is ProphetXGeolocationState.UNKNOWN_CONTRACT


def test_transport_failure_is_unknown_transport_without_error_text():
    admission = ProphetXGeolocationClient(
        FakeTransport(error=RuntimeError("Bearer SECRET")),
        clock=lambda: NOW,
    ).check(action(), "203.0.113.9")
    assert admission.state is ProphetXGeolocationState.UNKNOWN_TRANSPORT
    assert "SECRET" not in json.dumps(admission.to_durable_dict())


def test_action_binding_detects_economic_mutation(monkeypatch):
    monkeypatch.setattr(
        UrllibProphetXGeolocationTransport,
        "post",
        lambda self, endpoint, *, body, timeout_seconds: response(),
    )
    admission = ProphetXGeolocationClient(clock=lambda: NOW).check(
        action(),
        "203.0.113.9",
    )
    with pytest.raises(ProphetXGeolocationError, match="different action"):
        assert_prophetx_geolocation_admission_authoritative(
            admission,
            action(requested_stake=Decimal("11")),
        )


def test_caller_constructed_or_replaced_evidence_cannot_mint_authority(
    monkeypatch,
):
    monkeypatch.setattr(
        UrllibProphetXGeolocationTransport,
        "post",
        lambda self, endpoint, *, body, timeout_seconds: response(),
    )
    original = ProphetXGeolocationClient(clock=lambda: NOW).check(
        action(),
        "203.0.113.9",
    )
    forged = replace(original)
    with pytest.raises(ProphetXGeolocationError, match="not issued"):
        assert_prophetx_geolocation_admission_authoritative(forged, action())

    field_names = (
        "adapter_id",
        "adapter_version",
        "environment",
        "action_id",
        "action_sha256",
        "request_id",
        "observed_at",
        "endpoint",
        "state",
        "source_payload_sha256",
    )
    built = ProphetXGeolocationAdmission(
        *[getattr(original, field) for field in field_names]
    )
    with pytest.raises(ProphetXGeolocationError, match="not issued"):
        assert_prophetx_geolocation_admission_authoritative(built, action())


def test_non_prophetx_action_rejected():
    with pytest.raises(ProphetXGeolocationError, match="ProphetX"):
        ProphetXGeolocationClient(
            FakeTransport(),
            clock=lambda: NOW,
        ).check(action(bookmaker_id="betfair"), "203.0.113.9")


def test_result_after_action_expiry_is_expired(monkeypatch):
    monkeypatch.setattr(
        UrllibProphetXGeolocationTransport,
        "post",
        lambda self, endpoint, *, body, timeout_seconds: response(),
    )
    admission = ProphetXGeolocationClient(
        clock=lambda: "2026-09-22T21:05:00+00:00",
    ).check(action(), "203.0.113.9")
    assert admission.state is ProphetXGeolocationState.EXPIRED


def test_response_arriving_after_expiry_cannot_keep_pre_request_allow(monkeypatch):
    monkeypatch.setattr(
        UrllibProphetXGeolocationTransport,
        "post",
        lambda self, endpoint, *, body, timeout_seconds: response(),
    )
    times = iter(
        (
            "2026-09-22T21:04:59+00:00",
            "2026-09-22T21:05:01+00:00",
        )
    )
    admission = ProphetXGeolocationClient(clock=lambda: next(times)).check(
        action(),
        "203.0.113.9",
    )
    assert admission.state is ProphetXGeolocationState.EXPIRED
    assert admission.observed_at == "2026-09-22T21:05:01+00:00"


def test_transport_refuses_unqualified_origin():
    with pytest.raises(ProphetXGeolocationError, match="unqualified"):
        UrllibProphetXGeolocationTransport().post(
            "https://example.com/check-ip",
            body=b"{}",
            timeout_seconds=1,
        )


@pytest.mark.parametrize("timeout", [0, -1, 31, True])
def test_timeout_is_bounded(timeout):
    with pytest.raises(ValueError, match="timeout"):
        ProphetXGeolocationClient(
            FakeTransport(),
            timeout_seconds=timeout,
        )
