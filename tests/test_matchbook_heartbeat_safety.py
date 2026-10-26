from __future__ import annotations

import json
from urllib.error import HTTPError, URLError

import pytest

import autosport.matchbook_heartbeat_safety as mb

TOKEN = "session-token-value"
AT_1 = "2026-09-22T03:52:00Z"
AT_2 = "2026-09-22T03:53:00Z"
SHA_A = "a" * 64


class FakeHeaders(dict):
    pass


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.body = body
        self.status = status
        self.headers = FakeHeaders({"Content-Type": content_type})

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self.body
        return self.body[:size]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.fixture(autouse=True)
def clean_issued() -> None:
    mb._ISSUED_LEASES.clear()
    yield
    mb._ISSUED_LEASES.clear()


def install_transport(monkeypatch, responses, *, requests=None) -> None:
    queue = list(responses)
    seen = [] if requests is None else requests

    def fake_open(request, *, timeout):
        seen.append((request, timeout))
        if not queue:
            raise AssertionError("unexpected extra heartbeat request")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(mb, "_open_heartbeat_request", fake_open)


def install_clocks(monkeypatch, *, utc=AT_1, monotonic=100.0) -> None:
    monkeypatch.setattr(mb, "_utc_now", lambda: utc)
    monkeypatch.setattr(mb, "_monotonic_now", lambda: monotonic)


def capture(monkeypatch, *, requested=20, effective=30, generation=7, requests=None):
    install_clocks(monkeypatch)
    install_transport(
        monkeypatch,
        [FakeResponse(json.dumps({"timeout": effective}).encode())],
        requests=requests,
    )
    return mb.capture_matchbook_heartbeat(
        session_token=TOKEN,
        session_generation=generation,
        requested_timeout_seconds=requested,
    )


def test_capture_uses_fixed_canonical_post_and_keeps_token_out_of_url_body(monkeypatch) -> None:
    requests = []
    lease = capture(monkeypatch, requested=11, effective=29, requests=requests)
    request, timeout = requests[0]
    assert request.full_url == "https://api.matchbook.com/edge/rest/v1/heartbeat"
    assert request.get_method() == "POST"
    assert request.data == b'{"timeout":11}'
    assert TOKEN not in request.full_url
    assert TOKEN.encode() not in request.data
    headers = dict(request.header_items())
    assert headers["Session-token"] == TOKEN
    assert timeout == 10.0
    assert lease.registration.requested_timeout_seconds == 11
    assert lease.registration.effective_timeout_seconds == 29


def test_provider_effective_timeout_not_requested_timeout_controls_expiry(monkeypatch) -> None:
    lease = capture(monkeypatch, requested=20, effective=45)
    assert lease.expires_monotonic == 145.0
    assert lease.state(now_monotonic=144.999, current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.ACTIVE
    assert lease.state(now_monotonic=145.0, current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.EXPIRED


def test_shorter_provider_timeout_fails_closed(monkeypatch) -> None:
    lease = capture(monkeypatch, requested=60, effective=10)
    assert lease.state(now_monotonic=109.0, current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.ACTIVE
    assert lease.state(now_monotonic=110.0, current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.EXPIRED


def test_direct_caller_constructed_lease_cannot_mint_positive_authority() -> None:
    reg = mb.MatchbookHeartbeatRegistration(
        session_generation=7,
        requested_timeout_seconds=20,
        effective_timeout_seconds=9999,
        registered_at=AT_1,
        request_payload_sha256=SHA_A,
        provider_response_sha256=SHA_A,
    )
    forged = mb.MatchbookHeartbeatLease(registration=reg, registered_monotonic=100.0)
    with pytest.raises(mb.MatchbookHeartbeatSafetyError, match="not issued"):
        forged.state(now_monotonic=101.0, current_session_generation=7)


def test_public_api_has_no_primitive_success_issuer() -> None:
    assert not hasattr(mb, "register_after_provider_success")
    assert "_issue_lease" not in mb.__all__


def test_persisted_registration_after_restart_cannot_recreate_active_lease(monkeypatch) -> None:
    lease = capture(monkeypatch)
    persisted = lease.registration.to_dict()
    loaded = mb.MatchbookHeartbeatRegistration.from_dict(persisted)
    assert loaded.state_after_restart(current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.RESTART_REQUIRES_REREGISTRATION
    forged = mb.MatchbookHeartbeatLease(loaded, 100.0)
    with pytest.raises(mb.MatchbookHeartbeatSafetyError, match="not issued"):
        forged.state(now_monotonic=101.0, current_session_generation=7)


def test_session_rotation_invalidates_exact_issued_lease(monkeypatch) -> None:
    lease = capture(monkeypatch)
    assert lease.state(now_monotonic=101.0, current_session_generation=8) is mb.MatchbookHeartbeatLeaseState.SESSION_ROTATED
    assert lease.remaining_seconds(now_monotonic=101.0, current_session_generation=8) == 0.0


def test_monotonic_clock_rollback_fails_closed(monkeypatch) -> None:
    lease = capture(monkeypatch)
    with pytest.raises(mb.MatchbookHeartbeatSafetyError, match="moved backwards"):
        lease.state(now_monotonic=99.0, current_session_generation=7)


def test_wall_clock_audit_value_does_not_extend_lease(monkeypatch) -> None:
    lease = capture(monkeypatch, effective=10)
    # State is derived only from same-process monotonic time, not wall-clock text.
    monkeypatch.setattr(mb, "_utc_now", lambda: "2020-01-01T00:00:00Z")
    assert lease.state(now_monotonic=110.0, current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.EXPIRED


def test_refresh_uses_new_effective_timeout_and_supersedes_old_long_lease(monkeypatch) -> None:
    install_clocks(monkeypatch, utc=AT_1, monotonic=100.0)
    install_transport(monkeypatch, [FakeResponse(b'{"timeout":60}')])
    first = mb.capture_matchbook_heartbeat(
        session_token=TOKEN,
        session_generation=7,
        requested_timeout_seconds=60,
    )
    monkeypatch.setattr(mb, "_utc_now", lambda: AT_2)
    monkeypatch.setattr(mb, "_monotonic_now", lambda: 110.0)
    install_transport(monkeypatch, [FakeResponse(b'{"timeout":10}')])
    second = mb.refresh_matchbook_heartbeat(
        first,
        session_token=TOKEN,
        current_session_generation=7,
        requested_timeout_seconds=60,
    )
    assert first.state(now_monotonic=111.0, current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.SUPERSEDED
    assert second.registration.predecessor_registration_id == first.registration.registration_id
    assert second.expires_monotonic == 120.0
    assert second.state(now_monotonic=120.0, current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.EXPIRED


def test_refresh_transport_failure_degrades_old_positive_authority(monkeypatch) -> None:
    lease = capture(monkeypatch)
    monkeypatch.setattr(mb, "_monotonic_now", lambda: 105.0)
    install_transport(monkeypatch, [URLError("network unavailable")])
    with pytest.raises(mb.MatchbookHeartbeatTransportError, match="transport failed"):
        mb.refresh_matchbook_heartbeat(
            lease,
            session_token=TOKEN,
            current_session_generation=7,
            requested_timeout_seconds=20,
        )
    assert lease.state(now_monotonic=106.0, current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.DEGRADED_UNKNOWN


def test_refresh_monotonic_rollback_degrades_old_authority(monkeypatch) -> None:
    lease = capture(monkeypatch)
    monkeypatch.setattr(mb, "_monotonic_now", lambda: 99.0)
    install_transport(monkeypatch, [FakeResponse(b'{"timeout":20}')])
    with pytest.raises(mb.MatchbookHeartbeatSafetyError, match="moved backwards"):
        mb.refresh_matchbook_heartbeat(
            lease,
            session_token=TOKEN,
            current_session_generation=7,
            requested_timeout_seconds=20,
        )
    assert lease.state(now_monotonic=101.0, current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.DEGRADED_UNKNOWN


def test_refresh_cannot_cross_session_generation_without_transport(monkeypatch) -> None:
    lease = capture(monkeypatch)
    requests = []
    install_transport(monkeypatch, [FakeResponse(b'{"timeout":20}')], requests=requests)
    with pytest.raises(mb.MatchbookHeartbeatSafetyError, match="different session generation"):
        mb.refresh_matchbook_heartbeat(
            lease,
            session_token=TOKEN,
            current_session_generation=8,
            requested_timeout_seconds=20,
        )
    assert requests == []


def test_delete_heartbeat_is_unsubscribe_only_not_offer_cancel(monkeypatch) -> None:
    lease = capture(monkeypatch)
    requests = []
    monkeypatch.setattr(mb, "_utc_now", lambda: AT_2)
    install_transport(monkeypatch, [FakeResponse(b'{}')], requests=requests)
    evidence = mb.unsubscribe_matchbook_heartbeat(
        lease,
        session_token=TOKEN,
        current_session_generation=7,
    )
    request, _ = requests[0]
    assert request.get_method() == "DELETE"
    assert request.data is None
    assert evidence.offers_cancelled_proven is False
    assert evidence.to_dict()["offers_cancelled_proven"] is False
    assert lease.state(now_monotonic=101.0, current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.UNSUBSCRIBED
    assert mb.MatchbookHeartbeatUnsubscribeEvidence.from_dict(evidence.to_dict()) == evidence


def test_delete_failure_degrades_authority_and_mints_no_unsubscribe_evidence(monkeypatch) -> None:
    lease = capture(monkeypatch)
    install_transport(monkeypatch, [HTTPError(mb._HEARTBEAT_URL, 503, "down", None, None)])
    with pytest.raises(mb.MatchbookHeartbeatTransportError, match="unavailable"):
        mb.unsubscribe_matchbook_heartbeat(
            lease,
            session_token=TOKEN,
            current_session_generation=7,
        )
    assert lease.state(now_monotonic=101.0, current_session_generation=7) is mb.MatchbookHeartbeatLeaseState.DEGRADED_UNKNOWN


def test_heartbeat_expiry_observation_round_trips_durably() -> None:
    observation = mb.MatchbookHeartbeatCancellationObservation(
        offer_id=123,
        cancellation_reason=mb.MatchbookCancellationReason.HEARTBEAT_EXPIRY,
        observed_at=AT_2,
        provider_observation_sha256=SHA_A,
    )
    loaded = mb.MatchbookHeartbeatCancellationObservation.from_dict(observation.to_dict())
    assert loaded == observation
    assert loaded.heartbeat_expiry_observed is True
    assert observation.provider_origin_proven is False
    assert observation.offers_cancelled_proven is False
    assert loaded.provider_origin_proven is False
    assert loaded.offers_cancelled_proven is False


def test_user_request_is_not_relabelled_as_heartbeat_expiry() -> None:
    observation = mb.MatchbookHeartbeatCancellationObservation(
        offer_id=123,
        cancellation_reason=mb.MatchbookCancellationReason.USER_REQUEST,
        observed_at=AT_2,
        provider_observation_sha256=SHA_A,
    )
    assert observation.heartbeat_expiry_observed is False


def test_cancellation_observation_rejects_unknown_reason() -> None:
    raw = {
        "schema_version": 1,
        "provider": "matchbook",
        "offer_id": 123,
        "cancellation_reason": "some_other_reason",
        "observed_at": AT_2,
        "provider_observation_sha256": SHA_A,
    }
    with pytest.raises(ValueError, match="unsupported"):
        mb.MatchbookHeartbeatCancellationObservation.from_dict(raw)


def test_cancellation_observation_contains_no_exposure_or_retry_authority() -> None:
    names = set(mb.MatchbookHeartbeatCancellationObservation.__dataclass_fields__)
    assert names == {"offer_id", "cancellation_reason", "observed_at", "provider_observation_sha256"}
    for forbidden in ("matched", "remaining", "stake", "retry", "resubmit", "cancel_offer"):
        assert forbidden not in names


def test_heartbeat_module_exposes_no_betting_place_cancel_or_resubmit_api() -> None:
    public = set(mb.__all__)
    assert all("place" not in name.lower() for name in public)
    assert all("offer" not in name.lower() for name in public)
    assert all("resubmit" not in name.lower() for name in public)


def test_registration_persistence_excludes_token_account_and_monotonic(monkeypatch) -> None:
    lease = capture(monkeypatch)
    payload = lease.registration.to_dict()
    serialized = json.dumps(payload, sort_keys=True)
    assert TOKEN not in serialized
    assert "token" not in " ".join(payload).lower()
    assert "account" not in " ".join(payload).lower()
    assert "monotonic" not in " ".join(payload).lower()


def test_registration_digest_detects_material_tamper(monkeypatch) -> None:
    raw = capture(monkeypatch).registration.to_dict()
    raw["effective_timeout_seconds"] = 999
    with pytest.raises(ValueError, match="digest mismatch"):
        mb.MatchbookHeartbeatRegistration.from_dict(raw)


def test_registration_parser_rejects_unknown_secret_field(monkeypatch) -> None:
    raw = capture(monkeypatch).registration.to_dict()
    raw["session-token"] = TOKEN
    with pytest.raises(ValueError, match="fields mismatch"):
        mb.MatchbookHeartbeatRegistration.from_dict(raw)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "20"])
def test_requested_timeout_requires_positive_integer(monkeypatch, value) -> None:
    install_clocks(monkeypatch)
    install_transport(monkeypatch, [FakeResponse(b'{"timeout":20}')])
    with pytest.raises(ValueError, match="positive integer"):
        mb.capture_matchbook_heartbeat(
            session_token=TOKEN,
            session_generation=7,
            requested_timeout_seconds=value,
        )


@pytest.mark.parametrize("body", [b'{"timeout":true}', b'{"timeout":0}', b'{"timeout":1.5}', b'{}'])
def test_provider_timeout_requires_positive_integer(monkeypatch, body: bytes) -> None:
    install_clocks(monkeypatch)
    install_transport(monkeypatch, [FakeResponse(body)])
    with pytest.raises(mb.MatchbookHeartbeatTransportError, match="effective timeout"):
        mb.capture_matchbook_heartbeat(
            session_token=TOKEN,
            session_generation=7,
            requested_timeout_seconds=20,
        )


def test_duplicate_json_key_is_rejected(monkeypatch) -> None:
    install_clocks(monkeypatch)
    install_transport(monkeypatch, [FakeResponse(b'{"timeout":20,"timeout":30}')])
    with pytest.raises(mb.MatchbookHeartbeatTransportError, match="invalid JSON"):
        mb.capture_matchbook_heartbeat(
            session_token=TOKEN,
            session_generation=7,
            requested_timeout_seconds=20,
        )


def test_non_json_content_type_is_rejected(monkeypatch) -> None:
    install_clocks(monkeypatch)
    install_transport(monkeypatch, [FakeResponse(b'{"timeout":20}', content_type="text/plain")])
    with pytest.raises(mb.MatchbookHeartbeatTransportError, match="application/json"):
        mb.capture_matchbook_heartbeat(
            session_token=TOKEN,
            session_generation=7,
            requested_timeout_seconds=20,
        )


def test_oversize_response_is_rejected(monkeypatch) -> None:
    install_clocks(monkeypatch)
    body = b"x" * (mb._MAX_RESPONSE_BYTES + 1)
    install_transport(monkeypatch, [FakeResponse(body)])
    with pytest.raises(mb.MatchbookHeartbeatTransportError, match="bounded size"):
        mb.capture_matchbook_heartbeat(
            session_token=TOKEN,
            session_generation=7,
            requested_timeout_seconds=20,
        )


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (HTTPError(mb._HEARTBEAT_URL, 401, "bad", None, None), "authentication is unavailable"),
        (HTTPError(mb._HEARTBEAT_URL, 403, "bad", None, None), "authentication is unavailable"),
        (HTTPError(mb._HEARTBEAT_URL, 500, "bad", None, None), "provider is unavailable"),
        (URLError("down"), "transport failed"),
        (TimeoutError("down"), "transport failed"),
    ],
)
def test_transport_failures_are_sanitized_and_issue_no_lease(monkeypatch, failure, message) -> None:
    install_clocks(monkeypatch)
    install_transport(monkeypatch, [failure])
    with pytest.raises(mb.MatchbookHeartbeatTransportError, match=message) as caught:
        mb.capture_matchbook_heartbeat(
            session_token=TOKEN,
            session_generation=7,
            requested_timeout_seconds=20,
        )
    assert TOKEN not in str(caught.value)
    assert mb._ISSUED_LEASES == {}


def test_transport_exception_with_secret_text_does_not_chain_secret(monkeypatch) -> None:
    install_clocks(monkeypatch)
    install_transport(monkeypatch, [OSError(f"failed URL session-token={TOKEN}")])
    with pytest.raises(mb.MatchbookHeartbeatTransportError) as caught:
        mb.capture_matchbook_heartbeat(
            session_token=TOKEN,
            session_generation=7,
            requested_timeout_seconds=20,
        )
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None


def test_request_and_response_digests_bind_exact_success(monkeypatch) -> None:
    body = b'{"timeout":17}'
    install_clocks(monkeypatch)
    install_transport(monkeypatch, [FakeResponse(body)])
    lease = mb.capture_matchbook_heartbeat(
        session_token=TOKEN,
        session_generation=7,
        requested_timeout_seconds=20,
    )
    assert lease.registration.request_payload_sha256 == mb._digest_bytes(b'{"timeout":20}')
    assert lease.registration.provider_response_sha256 == mb._digest_bytes(body)


def test_noncanonical_utc_durable_record_is_rejected() -> None:
    with pytest.raises(ValueError, match="canonical"):
        mb.MatchbookHeartbeatRegistration(
            session_generation=7,
            requested_timeout_seconds=20,
            effective_timeout_seconds=20,
            registered_at="2026-09-22T05:52:00+02:00",
            request_payload_sha256=SHA_A,
            provider_response_sha256=SHA_A,
        )


def test_bool_session_generation_is_rejected_before_transport(monkeypatch) -> None:
    requests = []
    install_transport(monkeypatch, [FakeResponse(b'{"timeout":20}')], requests=requests)
    with pytest.raises(ValueError, match="positive integer"):
        mb.capture_matchbook_heartbeat(
            session_token=TOKEN,
            session_generation=True,
            requested_timeout_seconds=20,
        )
    assert requests == []


def test_bad_session_token_is_rejected_before_transport(monkeypatch) -> None:
    requests = []
    install_transport(monkeypatch, [FakeResponse(b'{"timeout":20}')], requests=requests)
    with pytest.raises(ValueError, match="session_token"):
        mb.capture_matchbook_heartbeat(
            session_token="bad token",
            session_generation=7,
            requested_timeout_seconds=20,
        )
    assert requests == []
